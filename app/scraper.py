from __future__ import annotations

import argparse
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from sqlmodel import Session, select
from webdriver_manager.chrome import ChromeDriverManager

from .db import engine, init_db
from .models import ScrapedRecipe

BASE_URL = "https://medlineplus.gov/spanish/recetas/"
LOGGER = logging.getLogger("scraper")
WAIT_TIMEOUT = 20


@dataclass
class Category:
    name: str
    url: str
    thumbnail_url: str | None = None


@dataclass
class RecipeCard:
    name: str
    url: str
    image_url: str | None


@dataclass
class RecipeDetails:
    name: str
    image_url: str | None
    servings_text: str | None
    ingredients: list[str]
    steps: list[str]
    nutrition: dict[str, str]


class RecipeScraper:
    def __init__(self, *, headless: bool = True, wait_timeout: int = WAIT_TIMEOUT) -> None:
        options = Options()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--lang=es-ES")
        options.add_argument("--window-size=1400,900")
        if headless:
            options.add_argument("--headless=new")

        try:
            self._driver = webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
                options=options,
            )
        except WebDriverException as exc:
            raise RuntimeError("No fue posible iniciar ChromeDriver") from exc

        self._wait = WebDriverWait(self._driver, wait_timeout)
        self._visited_recipes: set[str] = set()

    def close(self) -> None:
        with suppress(Exception):
            self._driver.quit()

    def scrape(self, session: Session, *, max_categories: int | None = None) -> None:
        main_soup = self._load_and_wait(BASE_URL)
        categories = list(self._parse_categories(main_soup))
        LOGGER.info("Detectadas %s categorías", len(categories))

        if max_categories is not None:
            categories = categories[:max_categories]

        for category in categories:
            LOGGER.info("Procesando categoría %s", category.name)
            soup = self._load_and_wait(category.url)
            cards = list(self._parse_recipe_cards(soup))
            LOGGER.info("Encontradas %s recetas en %s", len(cards), category.name)
            for card in cards:
                if card.url in self._visited_recipes:
                    LOGGER.debug("Saltando receta repetida %s", card.url)
                    continue
                details = self._scrape_recipe_detail(card.url)
                self._visited_recipes.add(card.url)
                self._persist_recipe(session, category, card, details)

    def _load_and_wait(self, url: str) -> BeautifulSoup:
        LOGGER.debug("Abriendo %s", url)
        self._driver.get(url)
        try:
            self._wait.until(EC.presence_of_element_located((By.TAG_NAME, "main")))
        except TimeoutException as exc:
            raise RuntimeError(f"Timeout esperando contenido principal en {url}") from exc
        return BeautifulSoup(self._driver.page_source, "html.parser")

    def _parse_categories(self, soup: BeautifulSoup) -> Iterator[Category]:
        seen: set[str] = set()
        for block in soup.select("div.recipe-card-block"):
            link = block.select_one("p strong a[href]")
            if not link:
                continue
            href = link.get("href")
            if not href:
                continue
            full_url = urljoin(BASE_URL, href)
            slug = self._slug_from_url(full_url)
            if slug in seen:
                continue
            seen.add(slug)
            img = block.select_one("img")
            thumb = urljoin(BASE_URL, img["src"]) if img and img.get("src") else None
            yield Category(name=link.get_text(strip=True), url=full_url, thumbnail_url=thumb)

    def _parse_recipe_cards(self, soup: BeautifulSoup) -> Iterator[RecipeCard]:
        for block in soup.select("div.recipe-card-block-with-height"):
            link = block.select_one("p strong a[href]")
            if not link:
                continue
            href = link.get("href")
            if not href:
                continue
            img = block.select_one("div.recipe-recog-img img")
            image_url = urljoin(BASE_URL, img["src"]) if img and img.get("src") else None
            yield RecipeCard(
                name=link.get_text(strip=True),
                url=urljoin(BASE_URL, href),
                image_url=image_url,
            )

    def _scrape_recipe_detail(self, url: str) -> RecipeDetails:
        soup = self._load_and_wait(url)
        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else url
        hero_img = soup.find("img", src=lambda value: value and "recipe_" in value)
        image_url = urljoin(BASE_URL, hero_img["src"]) if hero_img and hero_img.get("src") else None
        servings_text = self._extract_servings(soup)
        ingredients = self._extract_ingredients(soup)
        steps = self._extract_steps(soup)
        nutrition = self._extract_nutrition(soup)
        return RecipeDetails(
            name=name,
            image_url=image_url,
            servings_text=servings_text,
            ingredients=ingredients,
            steps=steps,
            nutrition=nutrition,
        )

    def _persist_recipe(
        self,
        session: Session,
        category: Category,
        card: RecipeCard,
        details: RecipeDetails,
    ) -> None:
        slug = self._slug_from_url(card.url)
        stmt = select(ScrapedRecipe).where(ScrapedRecipe.recipe_slug == slug)
        recipe = session.exec(stmt).first()
        timestamp = datetime.utcnow()
        payload = {
            "category_name": category.name,
            "category_url": category.url,
            "recipe_name": details.name,
            "recipe_slug": slug,
            "recipe_url": card.url,
            "hero_image_url": details.image_url or card.image_url,
            "source_thumbnail_url": card.image_url or category.thumbnail_url,
            "servings_text": details.servings_text,
            "ingredients": details.ingredients,
            "preparation_steps": details.steps,
            "nutrition_facts": details.nutrition,
            "updated_at": timestamp,
        }

        if recipe:
            for field, value in payload.items():
                setattr(recipe, field, value)
        else:
            recipe = ScrapedRecipe(**payload, created_at=timestamp)
            session.add(recipe)

        session.commit()

    def _extract_servings(self, soup: BeautifulSoup) -> str | None:
        for strong in soup.find_all("strong"):
            text = strong.get_text(strip=True)
            if text.lower().startswith("porciones"):
                tail = self._collect_sibling_text(strong)
                return f"{text} {tail}".strip()
        return None

    def _extract_ingredients(self, soup: BeautifulSoup) -> list[str]:
        header = soup.find("h3", string=lambda value: value and "ingred" in value.lower())
        if not header:
            return []
        ul = header.find_next("ul")
        if not ul:
            return []
        return [item.get_text(strip=True) for item in ul.find_all("li")]

    def _extract_steps(self, soup: BeautifulSoup) -> list[str]:
        header = soup.find("h3", string=lambda value: value and "prepar" in value.lower())
        if not header:
            return []
        ol = header.find_next("ol")
        if not ol:
            return []
        return [item.get_text(strip=True) for item in ol.find_all("li")]

    def _extract_nutrition(self, soup: BeautifulSoup) -> dict[str, str]:
        nutrition: dict[str, str] = {}
        container = soup.select_one("div.mp-nutrition")
        if not container:
            return nutrition
        span = container.find("span")
        if span and span.get_text(strip=True):
            nutrition["Porciones"] = span.get_text(strip=True)
        for row in container.select(".fact-row"):
            left = row.select_one(".fact-left") or row.find("span")
            right = row.select_one(".fact-right")
            label = left.get_text(" ", strip=True) if left else None
            value = right.get_text(" ", strip=True) if right else ""
            if label:
                nutrition[label] = value
        return nutrition

    def _collect_sibling_text(self, node: Tag) -> str:
        texts: list[str] = []
        for sibling in node.next_siblings:
            if isinstance(sibling, NavigableString):
                content = sibling.strip()
                if content:
                    texts.append(content)
                    break
            elif isinstance(sibling, Tag):
                content = sibling.get_text(" ", strip=True)
                if content:
                    texts.append(content)
                    break
        return " ".join(texts)

    @staticmethod
    def _slug_from_url(url: str) -> str:
        return url.rstrip("/").split("/")[-1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper de recetas MedlinePlus")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Ejecuta Chrome con interfaz visible",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita la cantidad de categorías a procesar",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Nivel de logging",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    init_db()
    scraper = RecipeScraper(headless=not args.headful)
    try:
        with Session(engine) as session:
            scraper.scrape(session, max_categories=args.limit)
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
