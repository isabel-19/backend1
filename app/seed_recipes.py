from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete
from sqlmodel import Session, select

from .db import engine, init_db
from .models import Recipe, RecipeIngredient, RecipeStep


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE = _BACKEND_DIR / "recetas_medlineplus.json"
_WORD_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_CATEGORY_LABELS = {
    "acompanamientos": "Acompañamientos",
    "almuerzo": "Almuerzo",
    "bajo en grasa": "Bajo en grasa",
    "bajo en sodio": "Bajo en sodio",
    "bebidas": "Bebidas",
    "bocadillos": "Bocadillos",
    "cena": "Cena",
    "desayuno": "Desayuno",
    "ensaladas": "Ensaladas",
    "pan": "Pan",
    "postres": "Postres",
    "salsas y aderezos": "Salsas y aderezos",
    "sin gluten": "Sin gluten",
    "sin lacteos": "Sin lácteos",
    "sopas": "Sopas",
    "vegetariana": "Vegetariana",
}


@dataclass(slots=True)
class SeedStats:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_duplicates: int = 0
    skipped_invalid: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_existing": self.skipped_existing,
            "skipped_duplicates": self.skipped_duplicates,
            "skipped_invalid": self.skipped_invalid,
        }


def _collapse_ws(value: str | None) -> str:
    if not value:
        return ""
    collapsed = _WORD_RE.sub(" ", value).strip()
    return collapsed


def _fold_text(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip().lower()
    text = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _normalize_category_label(value: str | None) -> str:
    raw = _collapse_ws(value)
    if not raw:
        return ""
    key = _fold_text(raw)
    return _CATEGORY_LABELS.get(key, raw)


def _merge_tags(existing: Iterable[str] | None, incoming: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tag in list(existing or []) + list(incoming):
        cleaned = _collapse_ws(str(tag))
        if not cleaned:
            continue
        key = _fold_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        merged.append(cleaned)
    return merged


def _should_update_primary_category(current: str | None, candidate: str | None) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    return _fold_text(current) == _fold_text(candidate) and current != candidate


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_value.lower()).strip("-")
    return slug or "receta"


def _find_calories(nutrition: dict[str, Any] | None) -> int | None:
    if not isinstance(nutrition, dict):
        return None
    for key, value in nutrition.items():
        if "calor" not in key.lower():
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            match = _NUMBER_RE.search(value)
            if match:
                try:
                    return int(float(match.group(1).replace(",", ".")))
                except ValueError:
                    return None
    return None


def _description_for(entry: dict[str, Any]) -> str | None:
    parts: list[str] = []
    servings = _collapse_ws(entry.get("porciones"))
    if servings:
        parts.append(f"Porciones: {servings}")
    source_url = _collapse_ws(entry.get("url"))
    if source_url:
        parts.append(f"Fuente: {source_url}")
    if not parts:
        return None
    return " | ".join(parts)


def _ensure_unique_slug(base_slug: str, taken: set[str]) -> str:
    slug = base_slug or "receta"
    counter = 2
    while slug in taken:
        slug = f"{base_slug}-{counter}" if base_slug else f"receta-{counter}"
        counter += 1
    taken.add(slug)
    return slug


def _load_source(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"No se encontro el archivo de recetas en {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict) and isinstance(data.get("recetas"), list):
        data = data.get("recetas")

    if not isinstance(data, list):
        raise ValueError("El archivo de recetas debe contener una lista de objetos")

    # Soporta formato agrupado por categoria: [{ categoria, recetas: [...] }, ...]
    flattened: list[dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("recetas"), list):
            category = entry.get("categoria")
            category_slug = entry.get("categoria_slug")
            category_url = entry.get("categoria_url")
            for recipe in entry.get("recetas") or []:
                if not isinstance(recipe, dict):
                    continue
                if category and not recipe.get("categoria"):
                    recipe["categoria"] = category
                if category_slug and not recipe.get("categoria_slug"):
                    recipe["categoria_slug"] = category_slug
                if category_url and not recipe.get("categoria_url"):
                    recipe["categoria_url"] = category_url
                flattened.append(recipe)
            continue
        if isinstance(entry, dict):
            flattened.append(entry)

    return flattened


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    normalized = []
    for tag in tags:
        cleaned = _collapse_ws(tag).lower()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized or ["medlineplus"]


def _create_ingredients(recipe_id: int, ingredients: list[str]) -> list[RecipeIngredient]:
    result: list[RecipeIngredient] = []
    for item in ingredients:
        text = _collapse_ws(item)
        if not text:
            continue
        result.append(RecipeIngredient(recipe_id=recipe_id, name=text))
    return result


def _create_steps(recipe_id: int, steps: list[str]) -> list[RecipeStep]:
    result: list[RecipeStep] = []
    for index, item in enumerate(steps, start=1):
        text = _collapse_ws(item)
        if not text:
            continue
        result.append(RecipeStep(recipe_id=recipe_id, position=index, instruction=text))
    return result


def seed_from_file(
    path: Path = _DEFAULT_SOURCE,
    *,
    dry_run: bool = False,
    replace_existing: bool = False,
    tags: Iterable[str] | None = None,
) -> dict[str, int]:
    source_path = path if isinstance(path, Path) else Path(path)
    records = _load_source(source_path)
    normalized_tags = _normalize_tags(tags or [])

    init_db()

    stats = SeedStats(total=len(records))
    with Session(engine) as session:
        existing_slugs = set(session.exec(select(Recipe.slug)).all() or [])
        taken_slugs = set(existing_slugs)
        handled_existing: set[str] = set()
        recipes_by_slug: dict[str, Recipe] = {}

        for entry in records:
            title = _collapse_ws(entry.get("nombre"))
            if not title:
                stats.skipped_invalid += 1
                continue

            base_slug = _slugify(title)
            category_label = _normalize_category_label(
                entry.get("categoria") or entry.get("category") or entry.get("tag")
            )
            recipe: Recipe | None = None

            # Evita duplicados dentro de la misma importación (dataset con títulos repetidos).
            if base_slug in recipes_by_slug:
                stats.skipped_duplicates += 1
                recipe = recipes_by_slug[base_slug]
                if category_label:
                    if _should_update_primary_category(recipe.tag, category_label):
                        recipe.tag = category_label
                    recipe.diet_tags = _merge_tags(recipe.diet_tags, [category_label])
                continue

            if base_slug in existing_slugs:
                if not replace_existing:
                    if base_slug not in handled_existing:
                        stats.skipped_existing += 1
                        handled_existing.add(base_slug)
                    recipe = session.exec(select(Recipe).where(Recipe.slug == base_slug)).first()
                    if recipe is None:
                        stats.skipped_invalid += 1
                        continue
                    if category_label:
                        if _should_update_primary_category(recipe.tag, category_label):
                            recipe.tag = category_label
                        recipe.diet_tags = _merge_tags(recipe.diet_tags, [category_label])
                    recipe.diet_tags = _merge_tags(recipe.diet_tags, normalized_tags)
                    recipes_by_slug[base_slug] = recipe
                    continue
                recipe = session.exec(select(Recipe).where(Recipe.slug == base_slug)).first()
                if recipe is None:
                    stats.skipped_invalid += 1
                    continue
                taken_slugs.add(base_slug)
            else:
                slug = _ensure_unique_slug(base_slug, taken_slugs)
                recipe = Recipe(slug=slug, title=title)
                session.add(recipe)

            recipe.title = title
            recipe.description = _description_for(entry)
            if category_label:
                if _should_update_primary_category(recipe.tag, category_label):
                    recipe.tag = category_label
            recipe.calories = _find_calories(entry.get("nutricion"))
            recipe.time_minutes = None
            image_url = _collapse_ws(entry.get("imagen"))
            recipe.image_url = image_url or None
            recipe.diet_tags = _merge_tags(recipe.diet_tags, normalized_tags)
            if category_label:
                recipe.diet_tags = _merge_tags(recipe.diet_tags, [category_label])

            session.flush()

            assert recipe.id is not None
            recipe_id = int(recipe.id)

            # Clean existing relations when replacing
            session.exec(delete(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe_id))  # type: ignore[arg-type]
            session.exec(delete(RecipeStep).where(RecipeStep.recipe_id == recipe_id))  # type: ignore[arg-type]

            ingredients_payload = entry.get("ingredientes") or []
            steps_payload = entry.get("preparacion") or []
            ingredients = _create_ingredients(recipe_id, list(ingredients_payload))
            steps = _create_steps(recipe_id, list(steps_payload))

            if ingredients:
                session.add_all(ingredients)
            if steps:
                session.add_all(steps)

            if base_slug in existing_slugs and replace_existing:
                stats.updated += 1
            else:
                stats.inserted += 1

            recipes_by_slug[base_slug] = recipe

        if dry_run:
            session.rollback()
        else:
            session.commit()

    return stats.as_dict()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Importa recetas desde el dataset de MedlinePlus")
    parser.add_argument(
        "--file",
        type=Path,
        default=_DEFAULT_SOURCE,
        help="Ruta al archivo JSON de recetas (utf-8)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simula la importacion sin escribir en la base de datos")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Actualiza recetas existentes con el mismo slug en lugar de omitirlas",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Etiqueta adicional para guardar en el campo diet_tags (puede usarse varias veces)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Nivel de detalle para los mensajes de registro",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(levelname)s: %(message)s")

    selected_tags = args.tag if args.tag is not None else ["medlineplus"]
    try:
        stats = seed_from_file(
            path=args.file,
            dry_run=args.dry_run,
            replace_existing=args.replace,
            tags=selected_tags,
        )
    except Exception as exc:  # pragma: no cover - CLI feedback path
        logging.error("No se pudieron importar las recetas: %s", exc)
        raise SystemExit(1) from exc

    logging.info(
        "Procesadas %s recetas (insertadas=%s, actualizadas=%s, omitidas=%s, inválidas=%s)%s",
        stats["total"],
        stats["inserted"],
        stats["updated"],
        stats["skipped_existing"],
        stats["skipped_invalid"],
        " [dry-run]" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
