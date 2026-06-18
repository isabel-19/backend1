from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import delete, func
from sqlmodel import Session, select

from .db import engine, init_db
from .models import (
    Recipe,
    RecipeIngredient,
    RecipeMacro,
    RecipeNutrition,
    RecipeStep,
    UserFavoriteRecipe,
    UserRecentRecipe,
    UserRecipeConsumption,
)


_WORD_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    text = _strip_accents(value).lower()
    text = _WORD_RE.sub(" ", text).strip()
    text = _NON_ALNUM_RE.sub("", text)
    text = _WORD_RE.sub(" ", text).strip()
    return text


def _is_placeholder_image(url: str | None) -> bool:
    if not url:
        return True
    trimmed = str(url).strip()
    if not trimmed:
        return True
    return "/uswds/img/us_flag_small.png" in trimmed


@dataclass(slots=True)
class DuplicateGroup:
    normalized_title: str
    recipe_ids: list[int]


@dataclass(slots=True)
class DedupeStats:
    total_recipes: int = 0
    duplicate_groups: int = 0
    duplicates_found: int = 0
    duplicates_deleted: int = 0


def _recipe_score(recipe: Recipe, *, ingredient_count: int, step_count: int) -> int:
    score = 0
    if recipe.description:
        score += 2
    if recipe.calories is not None:
        score += 1
    if recipe.time_minutes is not None:
        score += 1
    if not _is_placeholder_image(recipe.image_url):
        score += 3
    # prefer more complete detail
    score += min(10, ingredient_count)
    score += min(10, step_count)
    return score


def _pick_keep_id(
    recipes: list[Recipe],
    ingredient_counts: dict[int, int],
    step_counts: dict[int, int],
) -> int:
    ranked = []
    for recipe in recipes:
        rid = int(recipe.id)
        score = _recipe_score(
            recipe,
            ingredient_count=ingredient_counts.get(rid, 0),
            step_count=step_counts.get(rid, 0),
        )
        ranked.append((score, ingredient_counts.get(rid, 0), step_counts.get(rid, 0), -rid, rid))
    ranked.sort(reverse=True)
    return ranked[0][-1]


def find_duplicate_groups(recipes: Iterable[Recipe]) -> list[DuplicateGroup]:
    groups: dict[str, list[int]] = defaultdict(list)
    for recipe in recipes:
        key = _normalize_title(recipe.title)
        if not key:
            continue
        if recipe.id is None:
            continue
        groups[key].append(int(recipe.id))

    result: list[DuplicateGroup] = []
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        ids.sort()
        result.append(DuplicateGroup(normalized_title=key, recipe_ids=ids))

    result.sort(key=lambda g: (-len(g.recipe_ids), g.normalized_title))
    return result


def dedupe_recipes(*, apply: bool = False, log_examples: int = 8) -> DedupeStats:
    init_db()

    with Session(engine) as session:
        recipes = session.exec(select(Recipe)).all()

        ingredient_counts: dict[int, int] = {
            int(rid): int(count)
            for rid, count in session.exec(
                select(RecipeIngredient.recipe_id, func.count(RecipeIngredient.id)).group_by(RecipeIngredient.recipe_id)
            ).all()
        }

        step_counts: dict[int, int] = {
            int(rid): int(count)
            for rid, count in session.exec(
                select(RecipeStep.recipe_id, func.count(RecipeStep.id)).group_by(RecipeStep.recipe_id)
            ).all()
        }

        groups = find_duplicate_groups(recipes)

        stats = DedupeStats(
            total_recipes=len(recipes),
            duplicate_groups=len(groups),
            duplicates_found=sum(max(0, len(g.recipe_ids) - 1) for g in groups),
            duplicates_deleted=0,
        )

        if not groups:
            logging.info("No se detectaron duplicados por título.")
            return stats

        # Log a few examples
        if log_examples:
            examples = groups[: max(0, log_examples)]
            for group in examples:
                logging.info("Duplicado '%s' -> ids=%s", group.normalized_title, group.recipe_ids)

        if not apply:
            logging.warning(
                "Dry-run: se encontraron %s duplicados en %s grupos. Usa --apply para eliminarlos.",
                stats.duplicates_found,
                stats.duplicate_groups,
            )
            return stats

        # Build id->Recipe lookup for scoring
        by_id: dict[int, Recipe] = {int(r.id): r for r in recipes if r.id is not None}

        delete_ids: list[int] = []
        for group in groups:
            group_recipes = [by_id[rid] for rid in group.recipe_ids if rid in by_id]
            if len(group_recipes) < 2:
                continue
            keep_id = _pick_keep_id(group_recipes, ingredient_counts, step_counts)
            for rid in group.recipe_ids:
                if rid != keep_id:
                    delete_ids.append(rid)

        if not delete_ids:
            logging.info("No hubo ids concretos para eliminar.")
            return stats

        # Delete children first (FK constraints)
        for rid in delete_ids:
            session.exec(delete(RecipeIngredient).where(RecipeIngredient.recipe_id == rid))
            session.exec(delete(RecipeStep).where(RecipeStep.recipe_id == rid))
            session.exec(delete(RecipeMacro).where(RecipeMacro.recipe_id == rid))
            session.exec(delete(RecipeNutrition).where(RecipeNutrition.recipe_id == rid))
            session.exec(delete(UserRecipeConsumption).where(UserRecipeConsumption.recipe_id == rid))
            session.exec(delete(UserFavoriteRecipe).where(UserFavoriteRecipe.recipe_id == rid))
            session.exec(delete(UserRecentRecipe).where(UserRecentRecipe.recipe_id == rid))
            session.exec(delete(Recipe).where(Recipe.id == rid))

        session.commit()
        stats.duplicates_deleted = len(delete_ids)
        logging.info("Se eliminaron %s recetas duplicadas.", stats.duplicates_deleted)
        return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Elimina recetas duplicadas en la base de datos (por título normalizado)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica borrado de duplicados (por defecto es dry-run)",
    )
    parser.add_argument(
        "--log-examples",
        type=int,
        default=8,
        help="Cuántos grupos de ejemplo mostrar en logs",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Nivel de logs",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    stats = dedupe_recipes(apply=bool(args.apply), log_examples=max(0, int(args.log_examples)))
    logging.info(
        "Resumen: total=%s, grupos=%s, duplicados=%s, eliminados=%s",
        stats.total_recipes,
        stats.duplicate_groups,
        stats.duplicates_found,
        stats.duplicates_deleted,
    )


if __name__ == "__main__":
    main()
