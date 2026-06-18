from __future__ import annotations

from datetime import date, datetime, timedelta
from email.message import EmailMessage
import asyncio
import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path
import random
import re
import smtplib
from typing import Any
from urllib.parse import urlencode, urlparse
import unicodedata

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func
from sqlmodel import Session, delete, select
from collections import Counter, defaultdict

from .auth import create_access_token, decode_access_token
from .db import engine, get_session, init_db
from .models import (
    DailyFoodLog,
    Profile,
    Recipe,
    RecipeIngredient,
    RecipeMacro,
    RecipeNutrition,
    RecipeStep,
    UserFoodSwapEvent,
    UserCredential,
    User,
    UserHabitActivity,
    UserHabitBaseline,
    UserLoginEvent,
    UserWeightLog,
    UserRecipeConsumption,
    UserFavoriteRecipe,
    UserReminderSetting,
    UserRecentRecipe,
    WeeklyMeal,
    WeeklyPlan,
)
from .seed_recipes import seed_from_file
from .settings import settings


app = FastAPI(title="Recetas API")
logger = logging.getLogger(__name__)


def _build_cors_origins() -> list[str]:
    configured = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not configured:
        configured = ["http://localhost:9000"]

    origins: set[str] = set(configured)
    local_hosts = {"localhost", "127.0.0.1"}
    local_ports = {9000, 9001}

    for origin in list(configured):
        parsed = urlparse(origin)
        host = (parsed.hostname or "").strip().lower()
        scheme = parsed.scheme or "http"
        if host in local_hosts:
            for candidate_host in local_hosts:
                for port in local_ports:
                    origins.add(f"{scheme}://{candidate_host}:{port}")

    return sorted(origins)


def _seed_recipes_if_needed() -> None:
    try:
        with Session(engine) as session:
            total = session.exec(select(func.count(Recipe.id))).one()
    except Exception:
        return

    if total and total > 0:
        return

    source_path = Path(__file__).resolve().parents[1] / "recetas_medlineplus.json"
    try:
        seed_from_file(path=source_path, dry_run=False, replace_existing=False, tags=["medlineplus"])
    except Exception:
        # No bloquear el arranque si el seed falla.
        return


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _seed_recipes_if_needed()

# Authlib usa request.session para almacenar state/nonce en el flujo OAuth
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.jwt_secret,
    session_cookie="oauth_session",
    same_site=settings.cookie_samesite,
    https_only=bool(settings.cookie_secure),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

oauth = OAuth()
# OpenID Connect metadata de Google
oauth.register(
    name="google",
    client_id=settings.google_client_id or None,
    client_secret=settings.google_client_secret or None,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


COOKIE_NAME = "access_token"

_QUANTITY_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_UNIT_TO_GRAMS = {
    "kg": 1000.0,
    "g": 1.0,
    "gr": 1.0,
    "gramo": 1.0,
    "gramos": 1.0,
    "mg": 0.001,
    "ml": 1.0,
    "l": 1000.0,
    "cucharada": 15.0,
    "cucharadas": 15.0,
    "cucharadita": 5.0,
    "cucharaditas": 5.0,
    "taza": 240.0,
    "tazas": 240.0,
    "onza": 28.35,
    "onzas": 28.35,
}
_FOOD_NUTRITION_DB: dict[str, dict[str, float]] = {
    "pollo": {"calories": 165, "proteins": 31, "fats": 3.6, "carbohydrates": 0, "sugars": 0, "sodium_mg": 74},
    "huevo": {"calories": 155, "proteins": 13, "fats": 11, "carbohydrates": 1.1, "sugars": 1.1, "sodium_mg": 124},
    "arroz": {"calories": 130, "proteins": 2.7, "fats": 0.3, "carbohydrates": 28, "sugars": 0.1, "sodium_mg": 1},
    "papa": {"calories": 77, "proteins": 2, "fats": 0.1, "carbohydrates": 17, "sugars": 0.8, "sodium_mg": 6},
    "avena": {"calories": 389, "proteins": 16.9, "fats": 6.9, "carbohydrates": 66, "sugars": 0.9, "sodium_mg": 2},
    "leche": {"calories": 61, "proteins": 3.2, "fats": 3.3, "carbohydrates": 4.8, "sugars": 5, "sodium_mg": 43},
    "queso": {"calories": 402, "proteins": 25, "fats": 33, "carbohydrates": 1.3, "sugars": 0.5, "sodium_mg": 621},
    "yogur": {"calories": 59, "proteins": 10, "fats": 0.4, "carbohydrates": 3.6, "sugars": 3.2, "sodium_mg": 36},
    "frijol": {"calories": 127, "proteins": 8.7, "fats": 0.5, "carbohydrates": 23, "sugars": 0.3, "sodium_mg": 1},
    "lenteja": {"calories": 116, "proteins": 9, "fats": 0.4, "carbohydrates": 20, "sugars": 1.8, "sodium_mg": 2},
    "garbanzo": {"calories": 164, "proteins": 8.9, "fats": 2.6, "carbohydrates": 27, "sugars": 4.8, "sodium_mg": 7},
    "tomate": {"calories": 18, "proteins": 0.9, "fats": 0.2, "carbohydrates": 3.9, "sugars": 2.6, "sodium_mg": 5},
    "cebolla": {"calories": 40, "proteins": 1.1, "fats": 0.1, "carbohydrates": 9.3, "sugars": 4.2, "sodium_mg": 4},
    "zanahoria": {"calories": 41, "proteins": 0.9, "fats": 0.2, "carbohydrates": 10, "sugars": 4.7, "sodium_mg": 69},
    "brocoli": {"calories": 34, "proteins": 2.8, "fats": 0.4, "carbohydrates": 6.6, "sugars": 1.7, "sodium_mg": 33},
    "espinaca": {"calories": 23, "proteins": 2.9, "fats": 0.4, "carbohydrates": 3.6, "sugars": 0.4, "sodium_mg": 79},
    "manzana": {"calories": 52, "proteins": 0.3, "fats": 0.2, "carbohydrates": 14, "sugars": 10, "sodium_mg": 1},
    "platano": {"calories": 89, "proteins": 1.1, "fats": 0.3, "carbohydrates": 23, "sugars": 12, "sodium_mg": 1},
    "banana": {"calories": 89, "proteins": 1.1, "fats": 0.3, "carbohydrates": 23, "sugars": 12, "sodium_mg": 1},
    "aceite": {"calories": 884, "proteins": 0, "fats": 100, "carbohydrates": 0, "sugars": 0, "sodium_mg": 0},
    "mantequilla": {"calories": 717, "proteins": 0.9, "fats": 81, "carbohydrates": 0.1, "sugars": 0.1, "sodium_mg": 11},
    "azucar": {"calories": 387, "proteins": 0, "fats": 0, "carbohydrates": 100, "sugars": 100, "sodium_mg": 1},
    "sal": {"calories": 0, "proteins": 0, "fats": 0, "carbohydrates": 0, "sugars": 0, "sodium_mg": 38758},
}

_FOOD_COST_DB: dict[str, float] = {
    "pollo": 1.8,
    "huevo": 0.35,
    "arroz": 0.45,
    "papa": 0.3,
    "avena": 0.5,
    "leche": 0.55,
    "queso": 1.2,
    "yogur": 0.8,
    "frijol": 0.5,
    "lenteja": 0.55,
    "garbanzo": 0.65,
    "tomate": 0.25,
    "cebolla": 0.2,
    "zanahoria": 0.22,
    "brocoli": 0.75,
    "espinaca": 0.65,
    "manzana": 0.45,
    "platano": 0.35,
    "banana": 0.35,
    "aceite": 0.18,
    "mantequilla": 0.24,
    "azucar": 0.08,
    "sal": 0.03,
}

_UNHEALTHY_SWAPS: dict[str, dict[str, str]] = {
    "papas fritas": {"swap": "chips de garbanzo al horno", "reason": "menos grasa saturada y más fibra"},
    "gaseosa": {"swap": "agua con limón y hierbabuena", "reason": "reduce los azúcares añadidos"},
    "pan blanco": {"swap": "pan integral", "reason": "aporta más fibra y mejor saciedad"},
    "mayonesa": {"swap": "yogur natural con limón", "reason": "reduce grasas y calorías"},
    "salchicha": {"swap": "pollo desmechado", "reason": "menos sodio y ultraprocesados"},
    "tocino": {"swap": "pavo horneado", "reason": "menos grasa saturada"},
    "helado": {"swap": "yogur griego con fruta", "reason": "menos azúcar y más proteína"},
    "chocolate": {"swap": "fruta con cacao 70%", "reason": "menos azúcar refinada"},
    "postre": {"swap": "fruta fresca con nueces", "reason": "mejor calidad nutricional"},
    "fritura": {"swap": "horneado con especias", "reason": "menos aceite y calorías"},
}

_VALID_MEAL_TYPES = {"breakfast", "snack", "lunch", "dinner"}


class FoodRestrictionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "ninguna"
    items: list[str] = Field(default_factory=list)
    other_text: str | None = None


class ProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    birth_date: str | date | None = None
    sex: str | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    activity_level: str | None = None
    health_goal: str | None = None
    health_goal_other: str | None = None
    goal: str | None = None
    goal_message: str | None = None
    target_weight_kg: float | None = Field(default=None, ge=1, le=500)
    target_date: str | date | None = None
    motto: str | None = None
    food_restrictions: FoodRestrictionPayload = Field(default_factory=FoodRestrictionPayload)


class GoalProgressResponse(BaseModel):
    status: str
    label: str
    goal_type: str | None = None
    progress_pct: float = 0
    initial_weight_kg: float | None = None
    current_weight_kg: float | None = None
    target_weight_kg: float | None = None
    remaining_kg: float | None = None
    last_recorded_at: datetime | None = None
    target_date: date | None = None
    message: str


class AuthPasswordPayload(BaseModel):
    email: str
    password: str
    full_name: str | None = None


class RecipeIngredientPayload(BaseModel):
    name: str
    quantity: str | None = None
    swap: str | None = None
    optional: bool = False


class RecipeStepPayload(BaseModel):
    position: int
    instruction: str


class RecipeSummaryResponse(BaseModel):
    id: int
    slug: str
    title: str
    description: str | None = None
    tag: str | None = None
    category: str | None = None
    calories: int | None = None
    time_minutes: int | None = None
    image_url: str | None = None
    diet_tags: list[str] = Field(default_factory=list)


class RecipeDetailResponse(RecipeSummaryResponse):
    ingredients: list[RecipeIngredientPayload]
    steps: list[RecipeStepPayload]


class RecipeListResponse(BaseModel):
    items: list[RecipeSummaryResponse]
    total: int
    skip: int
    limit: int


class RecipeCategoryResponse(BaseModel):
    label: str
    count: int


class FavoriteListResponse(BaseModel):
    items: list[RecipeSummaryResponse]


class RecentRecipeResponse(RecipeSummaryResponse):
    seen_at: datetime


class RecentListResponse(BaseModel):
    items: list[RecentRecipeResponse]


class WeeklyPlanDayResponse(BaseModel):
    day: str
    theme: str | None = None
    meals: dict[str, RecipeSummaryResponse | None] = Field(default_factory=dict)


class WeeklyPlanResponse(BaseModel):
    week_start_date: date
    days: list[WeeklyPlanDayResponse]


class ShoppingListItemResponse(BaseModel):
    name: str
    count: int
    quantities: list[str] = Field(default_factory=list)
    recipes: list[str] = Field(default_factory=list)
    category: str = "Otros"


class ShoppingListResponse(BaseModel):
    items: list[ShoppingListItemResponse]


class AssistantHistoryItem(BaseModel):
    role: str
    content: str


class AssistantChatRequest(BaseModel):
    message: str
    history: list[AssistantHistoryItem] = Field(default_factory=list)


class AssistantChatResponse(BaseModel):
    reply: str
    recipes: list[RecipeDetailResponse] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class NutritionCalculatePayload(BaseModel):
    recipe_id: int | None = None
    recipe_slug: str | None = None
    force_recalculate: bool = False


class NutritionDataResponse(BaseModel):
    calories: float
    proteins: float
    fats: float
    carbohydrates: float
    sugars: float
    sodium_mg: float
    nutritional_score: float
    source: str
    calculated_at: datetime


class NutritionCalculateResponse(BaseModel):
    recipe: RecipeSummaryResponse
    nutrition: NutritionDataResponse


class ConsumptionRegisterPayload(BaseModel):
    recipe_id: int | None = None
    recipe_slug: str | None = None
    fecha_consumo: datetime | None = None
    porcion: float = 1.0
    baseline: bool = False


class ConsumptionRecordResponse(BaseModel):
    id: int
    user_id: int
    recipe_id: int
    fecha_consumo: datetime
    porcion: float
    baseline: bool
    nutrition: NutritionDataResponse


class NutritionTrendPoint(BaseModel):
    period_start: date
    avg_score: float
    avg_calories: float


class UserNutritionSummaryResponse(BaseModel):
    records: int
    avg_calories: float
    avg_proteins: float
    avg_nutritional_score: float
    trend: list[NutritionTrendPoint] = Field(default_factory=list)


class ComparisonMetrics(BaseModel):
    records: int
    avg_calories: float
    avg_proteins: float
    avg_nutritional_score: float


class UserNutritionComparisonResponse(BaseModel):
    baseline: ComparisonMetrics
    after: ComparisonMetrics
    delta_avg_calories: float
    delta_avg_proteins: float
    delta_avg_nutritional_score: float


class HabitBaselinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fruit_frequency: str
    vegetable_frequency: str
    junk_food_frequency: str
    water_daily_glasses: float = Field(default=0, ge=0, le=30)
    meal_schedule: str | None = None


class HabitActivityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    recipe_id: int | None = None
    recipe_type: str | None = None
    interaction_type: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class HabitLevelResponse(BaseModel):
    label: str
    min_score: int
    max_score: int


class HabitScoreResponse(BaseModel):
    user_id: int
    baseline_hai: int
    after_hai: int
    baseline_level: HabitLevelResponse
    after_level: HabitLevelResponse


class HabitComparisonResponse(BaseModel):
    user_id: int
    baseline_avg_hai: float
    after_avg_hai: float
    improvement: float


class HabitGlobalReportResponse(BaseModel):
    total_users_with_baseline: int
    total_users_with_activity: int
    average_baseline_hai: float
    average_after_hai: float
    average_improvement: float


class HabitActivityItemResponse(BaseModel):
    id: int
    event_type: str
    recipe_id: int | None = None
    recipe_type: str | None = None
    interaction_type: str | None = None
    created_at: datetime


class HabitActivityHistoryResponse(BaseModel):
    user_id: int
    items: list[HabitActivityItemResponse] = Field(default_factory=list)


class DailyFoodLogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumed_at: datetime | None = None
    meal_type: str = "snack"
    food_name: str
    quantity: str | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    calories_estimate: float | None = Field(default=None, ge=0)
    is_healthy: bool = False
    notes: str | None = None


class DailyFoodLogResponse(BaseModel):
    id: int
    consumed_at: datetime
    meal_type: str
    food_name: str
    quantity: str | None = None
    estimated_cost: float | None = None
    calories_estimate: float | None = None
    is_healthy: bool
    notes: str | None = None


class DailyConsumptionSummaryResponse(BaseModel):
    total_logs: int
    healthy_logs: int
    unhealthy_logs: int
    healthy_ratio: float
    avg_daily_logs: float
    top_foods: list[str] = Field(default_factory=list)


class PlanGenerationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_start: date | None = None
    include_snacks: bool = True
    weekly_budget: float | None = Field(default=None, ge=0)
    health_focus: str | None = None


class PlanBudgetResponse(BaseModel):
    estimated_total_cost: float
    weekly_budget: float | None = None
    within_budget: bool


class SmartWeeklyPlanResponse(BaseModel):
    plan: WeeklyPlanResponse
    budget: PlanBudgetResponse


class FoodSwapSuggestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    recipe_id: int | None = None
    ingredient_names: list[str] = Field(default_factory=list)
    max_suggestions: int = Field(default=5, ge=1, le=20)


class FoodSwapSuggestion(BaseModel):
    original: str
    replacement: str
    reason: str


class FoodSwapSuggestResponse(BaseModel):
    suggestions: list[FoodSwapSuggestion] = Field(default_factory=list)


class FoodSwapRegisterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_food_log_id: int | None = None
    original_food: str
    suggested_food: str
    accepted: bool = False


class FoodSwapIndicatorResponse(BaseModel):
    total_suggestions: int
    accepted_suggestions: int
    substitution_rate: float


class HealthyFrequencyPoint(BaseModel):
    period_start: date
    healthy_count: int
    unhealthy_count: int
    healthy_ratio: float


class HealthyFrequencyReportResponse(BaseModel):
    days: int
    total_healthy: int
    total_unhealthy: int
    overall_healthy_ratio: float
    trend: list[HealthyFrequencyPoint] = Field(default_factory=list)


class ReminderSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    hours_without_log: int = Field(default=24, ge=6, le=168)
    email_override: str | None = None


class ReminderSettingsResponse(BaseModel):
    enabled: bool
    hours_without_log: int
    email: str
    can_send_email: bool
    last_email_sent_at: datetime | None = None


class ReminderCheckResponse(BaseModel):
    enabled: bool
    should_remind: bool
    hours_since_last_log: float
    email_sent: bool
    message: str


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _parse_fraction_or_float(value: str) -> float | None:
    token = value.strip().replace(",", ".")
    if not token:
        return None
    if "/" in token:
        parts = token.split("/", maxsplit=1)
        if len(parts) == 2:
            try:
                numerator = float(parts[0])
                denominator = float(parts[1])
                if denominator != 0:
                    return numerator / denominator
            except Exception:
                return None
    try:
        return float(token)
    except Exception:
        return None


def _quantity_to_grams(quantity: str | None) -> float:
    raw = (quantity or "").strip().lower()
    if not raw:
        return 100.0

    match = _QUANTITY_NUMBER_RE.search(raw)
    if not match:
        return 100.0

    number = _parse_fraction_or_float(match.group(1))
    if number is None:
        return 100.0

    for unit, gram_factor in _UNIT_TO_GRAMS.items():
        if unit in raw:
            return max(1.0, number * gram_factor)

    # Sin unidad explícita, usar porción moderada.
    return max(15.0, min(300.0, number * 100.0))


def _nutrition_score(*, proteins: float, sugars: float, fats: float, sodium_mg: float) -> float:
    sodium_component = sodium_mg / 1000.0
    return round((proteins * 2.0) - (sugars + fats + sodium_component), 2)


def _empty_nutrients() -> dict[str, float]:
    return {
        "calories": 0.0,
        "proteins": 0.0,
        "fats": 0.0,
        "carbohydrates": 0.0,
        "sugars": 0.0,
        "sodium_mg": 0.0,
    }


def _estimate_recipe_nutrients(session: Session, recipe: Recipe) -> dict[str, float]:
    ingredients = session.exec(select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)).all()
    total = _empty_nutrients()

    for ing in ingredients:
        name = _fold_text(ing.name)
        grams = _quantity_to_grams(ing.quantity)
        if not name:
            continue
        for token, nutrient_per_100g in _FOOD_NUTRITION_DB.items():
            if token not in name:
                continue
            factor = grams / 100.0
            for key in total:
                total[key] += nutrient_per_100g[key] * factor
            break

    if total["calories"] <= 0 and recipe.calories:
        total["calories"] = float(recipe.calories)

    recipe_macro = session.get(RecipeMacro, recipe.id)
    if recipe_macro:
        if recipe_macro.protein_g is not None:
            total["proteins"] = float(recipe_macro.protein_g)
        if recipe_macro.carbs_g is not None:
            total["carbohydrates"] = float(recipe_macro.carbs_g)
        if recipe_macro.fats_g is not None:
            total["fats"] = float(recipe_macro.fats_g)

    if total["sugars"] <= 0 and total["carbohydrates"] > 0:
        total["sugars"] = round(total["carbohydrates"] * 0.35, 2)

    if total["sodium_mg"] <= 0:
        total["sodium_mg"] = 120.0

    for key in total:
        total[key] = round(max(0.0, total[key]), 2)

    return total


def _estimate_recipe_cost(session: Session, recipe: Recipe) -> float:
    ingredients = session.exec(select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)).all()
    total = 0.0
    if not ingredients:
        return 2.5

    for ing in ingredients:
        name = _fold_text(ing.name)
        grams = _quantity_to_grams(ing.quantity)
        matched = False
        for token, unit_cost in _FOOD_COST_DB.items():
            if token in name:
                total += (grams / 100.0) * unit_cost
                matched = True
                break
        if not matched:
            total += 0.25

    return round(max(0.5, total), 2)


def _is_consumption_healthy(row: UserRecipeConsumption) -> bool:
    if row.nutritional_score >= 6:
        return True
    return bool(row.calories <= 550 and row.sugars <= 12 and row.sodium_mg <= 700)


def _healthy_focus_bounds(health_focus: str | None) -> tuple[int, int]:
    focus = _fold_text(health_focus)
    if any(token in focus for token in ["perder", "bajar", "peso", "grasa"]):
        return (120, 520)
    if any(token in focus for token in ["ganar", "musculo", "músculo", "volumen"]):
        return (350, 900)
    return (180, 700)


def _extract_swap_candidates(*, text: str, ingredient_names: list[str]) -> list[str]:
    merged = [text] + ingredient_names
    found: list[str] = []
    for item in merged:
        normalized = _fold_text(item)
        if not normalized:
            continue
        for unhealthy in _UNHEALTHY_SWAPS.keys():
            if unhealthy in normalized and unhealthy not in found:
                found.append(unhealthy)
    return found


def _can_send_email_reminder() -> bool:
    return bool(
        settings.smtp_host
        and settings.smtp_port
        and settings.smtp_from_email
    )


def _send_email_reminder(*, to_email: str, subject: str, body_text: str) -> None:
    if not _can_send_email_reminder():
        raise RuntimeError("SMTP no configurado")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg.set_content(body_text)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=12) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def _get_or_create_reminder_setting(session: Session, user_id: int) -> UserReminderSetting:
    row = session.exec(select(UserReminderSetting).where(UserReminderSetting.user_id == user_id)).first()
    if row is not None:
        return row

    row = UserReminderSetting(user_id=user_id, enabled=False, hours_without_log=24)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _latest_consumption_datetime(session: Session, user_id: int) -> datetime | None:
    recipe_last = session.exec(
        select(UserRecipeConsumption.consumed_at)
        .where(UserRecipeConsumption.user_id == user_id)
        .order_by(UserRecipeConsumption.consumed_at.desc())
        .limit(1)
    ).first()
    manual_last = session.exec(
        select(DailyFoodLog.consumed_at)
        .where(DailyFoodLog.user_id == user_id)
        .order_by(DailyFoodLog.consumed_at.desc())
        .limit(1)
    ).first()

    if recipe_last and manual_last:
        return max(recipe_last, manual_last)
    return recipe_last or manual_last


def _resolve_recipe(session: Session, *, recipe_id: int | None, recipe_slug: str | None) -> Recipe:
    recipe: Recipe | None = None
    if recipe_id is not None:
        recipe = session.get(Recipe, recipe_id)
    elif recipe_slug:
        recipe = session.exec(select(Recipe).where(Recipe.slug == recipe_slug)).first()

    if recipe is None:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    return recipe


def _get_or_create_recipe_nutrition(
    session: Session,
    *,
    recipe: Recipe,
    force_recalculate: bool = False,
) -> RecipeNutrition:
    existing = session.get(RecipeNutrition, recipe.id)
    if existing and not force_recalculate:
        return existing

    nutrients = _estimate_recipe_nutrients(session, recipe)
    now = datetime.utcnow()

    if existing is None:
        existing = RecipeNutrition(
            recipe_id=int(recipe.id),
            calories=nutrients["calories"],
            proteins=nutrients["proteins"],
            fats=nutrients["fats"],
            carbohydrates=nutrients["carbohydrates"],
            sugars=nutrients["sugars"],
            sodium_mg=nutrients["sodium_mg"],
            source="internal-db",
            calculated_at=now,
        )
    else:
        existing.calories = nutrients["calories"]
        existing.proteins = nutrients["proteins"]
        existing.fats = nutrients["fats"]
        existing.carbohydrates = nutrients["carbohydrates"]
        existing.sugars = nutrients["sugars"]
        existing.sodium_mg = nutrients["sodium_mg"]
        existing.source = "internal-db"
        existing.calculated_at = now

    session.add(existing)
    session.commit()
    session.refresh(existing)
    return existing


def _to_nutrition_response(nutrition: RecipeNutrition, *, portion: float = 1.0) -> NutritionDataResponse:
    factor = max(0.1, portion)
    calories = round(nutrition.calories * factor, 2)
    proteins = round(nutrition.proteins * factor, 2)
    fats = round(nutrition.fats * factor, 2)
    carbohydrates = round(nutrition.carbohydrates * factor, 2)
    sugars = round(nutrition.sugars * factor, 2)
    sodium_mg = round(nutrition.sodium_mg * factor, 2)
    score = _nutrition_score(proteins=proteins, sugars=sugars, fats=fats, sodium_mg=sodium_mg)
    return NutritionDataResponse(
        calories=calories,
        proteins=proteins,
        fats=fats,
        carbohydrates=carbohydrates,
        sugars=sugars,
        sodium_mg=sodium_mg,
        nutritional_score=score,
        source=nutrition.source,
        calculated_at=nutrition.calculated_at,
    )


def _comparison_metrics(records: list[UserRecipeConsumption]) -> ComparisonMetrics:
    if not records:
        return ComparisonMetrics(records=0, avg_calories=0.0, avg_proteins=0.0, avg_nutritional_score=0.0)

    count = len(records)
    return ComparisonMetrics(
        records=count,
        avg_calories=round(sum(item.calories for item in records) / count, 2),
        avg_proteins=round(sum(item.proteins for item in records) / count, 2),
        avg_nutritional_score=round(sum(item.nutritional_score for item in records) / count, 2),
    )


def _normalize_frequency(value: str | None) -> str:
    token = _fold_text(value)
    if token in {"diario", "siempre", "alta", "frecuente", "todos los dias", "todos los dias"}:
        return "high"
    if token in {"medio", "moderado", "regular", "a veces", "semanal"}:
        return "medium"
    return "low"


def _baseline_component_scores(baseline: UserHabitBaseline) -> dict[str, int]:
    fruit_score = 2 if _normalize_frequency(baseline.fruit_frequency) in {"high", "medium"} else 0
    vegetable_score = 2 if _normalize_frequency(baseline.vegetable_frequency) in {"high", "medium"} else 0
    water_score = 2 if baseline.water_daily_glasses >= 6 else 0
    junk_penalty = -2 if _normalize_frequency(baseline.junk_food_frequency) == "high" else 0
    return {
        "fruit": fruit_score,
        "vegetable": vegetable_score,
        "water": water_score,
        "junk": junk_penalty,
    }


def _scale_hai(raw_score: int) -> int:
    clamped = max(-2, min(6, raw_score))
    scaled = round(((clamped + 2) / 8) * 15)
    return int(max(0, min(15, scaled)))


def _hai_level(score: int) -> HabitLevelResponse:
    if score <= 5:
        return HabitLevelResponse(label="habitos deficientes", min_score=0, max_score=5)
    if score <= 10:
        return HabitLevelResponse(label="habitos regulares", min_score=6, max_score=10)
    return HabitLevelResponse(label="habitos saludables", min_score=11, max_score=15)


def _after_habit_raw_score(session: Session, user_id: int, baseline: UserHabitBaseline) -> int:
    activities = session.exec(
        select(UserHabitActivity)
        .where(UserHabitActivity.user_id == user_id)
        .order_by(UserHabitActivity.created_at.asc())
    ).all()

    app_open_count = sum(1 for act in activities if _fold_text(act.event_type) == "app_open")
    consumed_count = sum(1 for act in activities if _fold_text(act.event_type) == "consumed_recipe")
    recommendation_count = sum(
        1 for act in activities
        if _fold_text(act.event_type) == "recommendation_interaction"
    )

    fruit_like = 0
    vegetable_like = 0
    junk_like = 0
    healthy_recipe_like = 0
    unhealthy_recipe_like = 0

    for act in activities:
        rtype = _fold_text(act.recipe_type)
        if any(token in rtype for token in ["fruta", "fruit", "smoothie"]):
            fruit_like += 1
        if any(token in rtype for token in ["verdura", "vegetal", "ensalada", "vegetable"]):
            vegetable_like += 1
        if any(token in rtype for token in ["chatarra", "frito", "postre", "ultraprocesado", "snack"]):
            junk_like += 1

        if _fold_text(act.event_type) == "consumed_recipe":
            if any(token in rtype for token in ["ensalada", "sopa", "verdura", "saludable", "healthy"]):
                healthy_recipe_like += 1
            if any(token in rtype for token in ["frito", "postre", "chatarra", "snack"]):
                unhealthy_recipe_like += 1

    fruit_score = 2 if (fruit_like >= 3 or healthy_recipe_like >= 5) else 0
    vegetable_score = 2 if (vegetable_like >= 3 or healthy_recipe_like >= 5) else 0
    water_score = 2 if (app_open_count >= 10 or recommendation_count >= 5) else 0
    junk_penalty = -2 if (junk_like >= 4 or unhealthy_recipe_like >= 3 or consumed_count == 0) else 0

    baseline_raw = sum(_baseline_component_scores(baseline).values())
    activity_raw = fruit_score + vegetable_score + water_score + junk_penalty

    if activities:
        return activity_raw
    return baseline_raw


def _compute_habit_scores(session: Session, user_id: int) -> tuple[int, int]:
    baseline = session.exec(select(UserHabitBaseline).where(UserHabitBaseline.user_id == user_id)).first()
    if baseline is None:
        return 0, 0

    baseline_raw = sum(_baseline_component_scores(baseline).values())
    after_raw = _after_habit_raw_score(session, user_id, baseline)
    return _scale_hai(baseline_raw), _scale_hai(after_raw)


def _get_weekly_plan(session: Session, *, user_id: int, week_start_date: date) -> WeeklyPlan | None:
    stmt = select(WeeklyPlan).where(
        WeeklyPlan.user_id == user_id,
        WeeklyPlan.week_start_date == week_start_date,
    )
    return session.exec(stmt).first()


def _weekly_plan_to_response(session: Session, plan: WeeklyPlan, *, day_names: list[str]) -> WeeklyPlanResponse:
    meals = session.exec(select(WeeklyMeal).where(WeeklyMeal.weekly_plan_id == plan.id)).all()
    recipe_ids = [m.recipe_id for m in meals if m.recipe_id]
    recipes = session.exec(select(Recipe).where(Recipe.id.in_(recipe_ids))).all() if recipe_ids else []
    recipe_by_id = {int(r.id): r for r in recipes if r.id is not None}

    meals_by_day: dict[str, dict[str, RecipeSummaryResponse | None]] = {}
    for meal in meals:
        day = meal.day_of_week
        if not day:
            continue
        if day not in meals_by_day:
            meals_by_day[day] = {}

        key = (meal.meal_type or '').strip().lower()
        if key not in {"breakfast", "snack", "lunch", "dinner"}:
            continue

        recipe = recipe_by_id.get(int(meal.recipe_id)) if meal.recipe_id else None
        meals_by_day[day][key] = _recipe_to_summary(recipe) if recipe else None

    days: list[WeeklyPlanDayResponse] = []
    for day in day_names:
        days.append(
            WeeklyPlanDayResponse(
                day=day,
                theme=plan.theme,
                meals=meals_by_day.get(day, {}),
            )
        )

    return WeeklyPlanResponse(week_start_date=plan.week_start_date, days=days)


def _build_generated_weekly_plan(
    session: Session,
    *,
    user_id: int,
    week_start_date: date,
    include_snacks: bool,
    weekly_budget: float | None,
    health_focus: str | None,
) -> tuple[WeeklyPlanResponse, float]:
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    slots = ["breakfast", "lunch", "dinner"]
    if include_snacks:
        slots = ["breakfast", "snack", "lunch", "dinner"]

    plan = _get_weekly_plan(session, user_id=user_id, week_start_date=week_start_date)
    profile = _get_profile_for_user(session, user_id)
    theme_label = (health_focus or profile.goal or profile.health_goal or "Auto") if profile else (health_focus or "Auto")

    if plan is None:
        plan = WeeklyPlan(user_id=user_id, week_start_date=week_start_date, theme=theme_label)
        session.add(plan)
        session.commit()
        session.refresh(plan)
    else:
        plan.theme = theme_label
        session.add(plan)
        session.commit()

    session.exec(delete(WeeklyMeal).where(WeeklyMeal.weekly_plan_id == plan.id))
    session.commit()

    recipes = session.exec(select(Recipe).order_by(Recipe.title)).all()
    if not recipes:
        raise HTTPException(status_code=409, detail="No hay recetas para generar el plan")

    min_cal, max_cal = _healthy_focus_bounds(health_focus)
    filtered_by_goal = [
        recipe for recipe in recipes
        if recipe.calories is None or (min_cal <= int(recipe.calories) <= max_cal)
    ]
    candidate_pool = filtered_by_goal if filtered_by_goal else recipes

    preferred_categories = _preferred_categories_from_profile(profile)
    if preferred_categories:
        normalized_preferences = {
            _normalize_category_query(label)
            for label in preferred_categories
            if _normalize_category_query(label)
        }
        preferred_pool = [
            recipe
            for recipe in candidate_pool
            if normalized_preferences.intersection(
                {
                    _normalize_category_query(label)
                    for label in _categories_for_recipe(recipe)
                    if _normalize_category_query(label)
                }
            )
        ]
        if preferred_pool:
            candidate_pool = preferred_pool

    if weekly_budget is not None:
        cost_by_recipe = {int(r.id): _estimate_recipe_cost(session, r) for r in candidate_pool if r.id is not None}
        candidate_pool = sorted(candidate_pool, key=lambda r: cost_by_recipe.get(int(r.id), 99.0))
    else:
        rng = random.Random()
        rng.shuffle(candidate_pool)

    total_cost = 0.0
    idx = 0
    for day in day_names:
        for slot in slots:
            recipe = candidate_pool[idx % len(candidate_pool)]
            idx += 1
            if recipe.id is None:
                continue

            recipe_cost = _estimate_recipe_cost(session, recipe)
            if weekly_budget is not None and total_cost + recipe_cost > weekly_budget and len(candidate_pool) > 1:
                for alt in candidate_pool:
                    if alt.id is None:
                        continue
                    alt_cost = _estimate_recipe_cost(session, alt)
                    if total_cost + alt_cost <= weekly_budget:
                        recipe = alt
                        recipe_cost = alt_cost
                        break

            total_cost += recipe_cost
            session.add(
                WeeklyMeal(
                    weekly_plan_id=int(plan.id),
                    day_of_week=day,
                    meal_type=slot,
                    recipe_id=int(recipe.id),
                    notes=f"cost_estimate={recipe_cost}",
                )
            )

    session.commit()
    session.refresh(plan)
    return _weekly_plan_to_response(session, plan, day_names=day_names), round(total_cost, 2)


def _infer_recipe_category(title: str | None) -> str:
    raw = (title or "").lower()
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))

    def has_any(keywords: list[str]) -> bool:
        return any(keyword in text for keyword in keywords)

    if has_any([
        "postre",
        "tarta",
        "pastel",
        "galleta",
        "bizcocho",
        "brownie",
        "muffin",
        "donut",
        "helado",
        "pudin",
        "flan",
        "chocolate",
        "dulce",
        "mermelada",
        "cupcake",
        "crepa",
        "crepas",
        "waffle",
    ]):
        return "Postres"

    if has_any([
        "batido",
        "smoothie",
        "jugo",
        "zumo",
        "licuado",
        "bebida",
        "te ",
        "cafe",
        "chocolate caliente",
        "limonada",
    ]):
        return "Bebidas"

    if has_any([
        "ensalada",
        "ceviche",
        "pico de gallo",
        "tabule",
        "tabbouleh",
    ]):
        return "Ensaladas"

    if has_any([
        "sopa",
        "crema ",
        "caldo",
        "gazpacho",
        "consome",
    ]):
        return "Sopas"

    if has_any([
        "desayuno",
        "avena",
        "granola",
        "yogur",
        "yogurt",
        "tostada",
        "panqueque",
        "pancake",
        "huev",
        "omelet",
        "omelette",
        "hotcake",
    ]):
        return "Desayuno"

    if has_any([
        "snack",
        "botana",
        "dip",
        "hummus",
        "barra",
        "barrita",
        "tapas",
    ]):
        return "Snack"

    return "Sin categoría"


def _category_for_recipe(recipe: Recipe) -> str:
    categories = _categories_for_recipe(recipe)
    if categories:
        return categories[0]
    return _infer_recipe_category(recipe.title)


def _categories_for_recipe(recipe: Recipe) -> list[str]:
    categories: list[str] = []

    tag = (recipe.tag or "").strip()
    if tag:
        categories.append(tag)

    for diet_tag in recipe.diet_tags or []:
        cleaned = (diet_tag or "").strip()
        if not cleaned:
            continue
        normalized = _normalize_category_query(cleaned)
        if normalized == "medlineplus":
            continue
        categories.append(cleaned)

    seen: set[str] = set()
    unique: list[str] = []
    for label in categories:
        key = _normalize_category_query(label) or label.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(label)

    return unique


def _preferred_categories_from_profile(profile: Profile | None) -> list[str]:
    if profile is None:
        return []

    goal_text = " ".join(
        part
        for part in [profile.goal, profile.health_goal, profile.health_goal_other]
        if part and str(part).strip()
    )
    normalized_goal = _fold_text(goal_text)

    preferred: list[str] = []

    def add_labels(labels: list[str]) -> None:
        for label in labels:
            key = _normalize_category_query(label)
            if not key:
                continue
            if key not in { _normalize_category_query(l) for l in preferred }:
                preferred.append(label)

    if any(word in normalized_goal for word in ["bajar", "perder", "peso", "grasa", "adelgazar"]):
        add_labels(["Bajo en grasa", "Bajo en sodio", "Ensaladas", "Sopas"])
    elif any(word in normalized_goal for word in ["musculo", "músculo", "ganar", "fuerza", "volumen"]):
        add_labels(["Desayuno", "Almuerzo", "Cena"])
    elif any(word in normalized_goal for word in ["energia", "energía", "rendimiento"]):
        add_labels(["Desayuno", "Almuerzo", "Bocadillos"])

    restrictions = [item for item in (profile.food_restriction_items or []) if str(item).strip()]
    restriction_map = {
        "sin gluten": "Sin gluten",
        "gluten": "Sin gluten",
        "sin lacteos": "Sin lácteos",
        "sin lácteos": "Sin lácteos",
        "lacteos": "Sin lácteos",
        "lácteos": "Sin lácteos",
        "vegetariana": "Vegetariana",
        "vegetariano": "Vegetariana",
    }
    for item in restrictions:
        key = _normalize_category_query(item)
        label = restriction_map.get(key or "", None)
        if label:
            add_labels([label])

    return preferred


def _normalize_category_query(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))

    aliases = {
        "snak": "snack",
        "snacks": "snack",
        "meriendas": "snack",
        "merienda": "snack",
    }
    return aliases.get(text, text)


def _fold_text(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip().lower()
    text = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _extract_time_limit_minutes(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*(min|minuto|minutos)", text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except Exception:
        return None
    return min(max(value, 5), 240)


def _assistant_keywords() -> dict[str, list[str]]:
    return {
        "desayuno": ["desayuno", "desayunar", "mañana", "manana"],
        "almuerzo": ["almuerzo", "comida", "mediodia", "mediodía"],
        "cena": ["cena", "cenar", "noche"],
        "vegetariana": ["vegetariano", "vegetariana", "veggie"],
        "vegana": ["vegano", "vegana"],
        "sin gluten": ["sin gluten", "gluten"],
        "sin lácteos": ["sin lacteos", "sin lácteos", "lactosa", "lacteos", "lácteos"],
        "ensaladas": ["ensalada", "ensaladas"],
        "sopas": ["sopa", "sopas", "caldo", "crema"],
        "postres": ["postre", "postres", "dulce"],
        "bajo en grasa": ["bajo en grasa", "ligero", "light"],
    }


def _should_attach_recipe_context(
    *,
    message: str,
    categories: list[str],
    ingredients: list[str],
    time_limit: int | None,
    calorie_limit: int | None,
) -> bool:
    if categories or ingredients or time_limit or calorie_limit:
        return True

    intent_tokens = [
        "receta",
        "recetas",
        "cocinar",
        "preparo",
        "preparar",
        "comer",
        "desayuno",
        "almuerzo",
        "cena",
        "menu",
        "menú",
        "ingredientes",
    ]
    return any(token in message for token in intent_tokens)


def _should_show_recipe_cards(
    *,
    message: str,
    categories: list[str],
    ingredients: list[str],
    time_limit: int | None,
    calorie_limit: int | None,
) -> bool:
    normalized = _fold_text(message)
    if not normalized:
        return False

    explicit_tokens = [
        "receta",
        "recetas",
        "que preparo",
        "que cocino",
        "que puedo cocinar",
        "quiero comer",
        "quiero preparar",
        "dame opciones",
        "dame ideas",
        "ingredientes",
        "menu",
        "menú",
        "desayuno",
        "almuerzo",
        "cena",
        "preparacion",
        "preparación",
        "pasos",
    ]

    if categories or ingredients or time_limit or calorie_limit:
        return True

    return any(token in normalized for token in explicit_tokens)


def _detect_requested_categories(text: str, available_categories: set[str]) -> list[str]:
    normalized_available = {
        _normalize_category_query(category): category
        for category in available_categories
        if _normalize_category_query(category)
    }

    detected: list[str] = []
    for canonical, words in _assistant_keywords().items():
        if any(_fold_text(word) in text for word in words):
            key = _normalize_category_query(canonical)
            if key and key in normalized_available:
                detected.append(normalized_available[key])

    # Match exact category names from DB when user writes them directly.
    for raw_key, original in normalized_available.items():
        if raw_key and raw_key in text and original not in detected:
            detected.append(original)

    return detected


def _detect_calorie_limit(text: str) -> int | None:
    match = re.search(r"(\d{2,4})\s*(kcal|calorias|calorías)", text)
    if match:
        try:
            value = int(match.group(1))
            return min(max(value, 80), 2000)
        except Exception:
            return None

    if any(token in text for token in ["bajas calorias", "bajo en calorias", "bajo en calorías", "light"]):
        return 450

    return None


def _build_recipe_ingredient_index(session: Session) -> dict[int, set[str]]:
    rows = session.exec(select(RecipeIngredient.recipe_id, RecipeIngredient.name)).all()
    by_recipe: dict[int, set[str]] = defaultdict(set)

    for recipe_id, ingredient_name in rows:
        if not recipe_id or not ingredient_name:
            continue
        normalized = _fold_text(ingredient_name)
        if not normalized:
            continue

        parts = [part.strip() for part in re.split(r"[,;/]|\s+", normalized) if part.strip()]
        by_recipe[int(recipe_id)].add(normalized)
        by_recipe[int(recipe_id)].update(parts)

    return by_recipe


def _extract_candidate_ingredients(text: str, ingredient_index: dict[int, set[str]]) -> list[str]:
    if not text:
        return []

    vocabulary: set[str] = set()
    for tokens in ingredient_index.values():
        for token in tokens:
            if len(token) >= 3:
                vocabulary.add(token)

    found = [token for token in vocabulary if token in text and len(token) >= 4]
    # Keep short list of most meaningful tokens.
    found = sorted(found, key=len, reverse=True)
    return found[:8]


def _assistant_search_terms(text: str, ingredients: list[str]) -> list[str]:
    normalized = _fold_text(text)
    if not normalized:
        return list(ingredients[:8])

    alias_map = {
        "tallarin": ["tallarin", "tallarines", "pasta", "fideo", "espagueti"],
        "tallarines": ["tallarin", "tallarines", "pasta", "fideo", "espagueti"],
        "pasta": ["pasta", "fideo", "espagueti", "manicotti", "penne", "rotini", "orzo"],
        "fideo": ["fideo", "fideos", "pasta", "ramen"],
        "fideos": ["fideo", "fideos", "pasta", "ramen"],
        "espagueti": ["espagueti", "pasta", "tallarin"],
        "espaguetis": ["espagueti", "pasta", "tallarin"],
        "ramen": ["ramen", "fideo", "pasta"],
        "salsa": ["salsa", "tomate", "pasta"],
    }
    stopwords = {
        "quiero", "comer", "algo", "para", "con", "sin", "una", "unos", "unas", "que",
        "como", "hoy", "favor", "por", "las", "los", "del", "de", "me", "da", "dame",
        "preparar", "preparo", "cocinar", "receta", "recetas",
    }

    terms: list[str] = []
    for token in re.findall(r"[a-zA-Záéíóúñü]+", normalized):
        folded = _fold_text(token)
        if len(folded) < 4 or folded in stopwords:
            continue
        terms.append(folded)
        terms.extend(alias_map.get(folded, []))

    for ingredient in ingredients:
        folded = _fold_text(ingredient)
        if folded:
            terms.append(folded)
            terms.extend(alias_map.get(folded, []))

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        folded = _fold_text(term)
        if len(folded) < 4 or folded in seen:
            continue
        seen.add(folded)
        unique_terms.append(folded)

    return unique_terms[:16]


def _is_exercise_request(text: str) -> bool:
    normalized = _fold_text(text)
    if not normalized:
        return False

    exercise_tokens = [
        "ejercicio", "ejercicios", "rutina", "rutinas", "entrenar", "entrenamiento", "actividad fisica",
        "actividad física", "caminar", "cardio", "fuerza", "movilidad", "estiramiento", "pilates", "yoga",
    ]
    return any(token in normalized for token in exercise_tokens)


def _is_preparation_request(text: str) -> bool:
    normalized = _fold_text(text)
    if not normalized:
        return False
    tokens = [
        "preparacion", "preparacion", "preparar", "como se prepara", "como preparo",
        "pasos", "instrucciones", "receta completa", "como hacerlo", "como hacerla",
    ]
    return any(token in normalized for token in tokens)


def _assistant_query_intent(message: str, search_terms: list[str]) -> dict[str, Any]:
    normalized = _fold_text(message)
    term_set = {_fold_text(term) for term in search_terms if _fold_text(term)}

    wants_side = any(token in normalized for token in [
        "acompanar", "acompañar", "guarnicion", "guarnición", "entrada", "dip", "aderezo", "salsa",
    ])
    wants_snack = any(token in normalized for token in ["snack", "botana", "bocadillo", "bocadillos", "merienda"])
    wants_drink = any(token in normalized for token in ["bebida", "jugo", "batido", "smoothie"])
    wants_dessert = any(token in normalized for token in ["postre", "dulce"])
    wants_breakfast = any(token in normalized for token in ["desayuno", "desayunar"])
    wants_lunch = any(token in normalized for token in ["almuerzo", "comida", "almorzar"])
    wants_dinner = any(token in normalized for token in ["cena", "cenar"])
    wants_main_dish = any(token in normalized for token in [
        "quiero comer", "almorzar", "cenar", "plato", "plato fuerte", "comida", "receta",
    ])
    if term_set.intersection({"pasta", "tallarin", "tallarines", "espagueti", "fideo", "ramen"}):
        wants_main_dish = True

    preferred_category_keys: set[str] = set()
    avoided_category_keys: set[str] = set()
    preferred_text_terms: set[str] = set(term_set)

    if term_set.intersection({"pasta", "tallarin", "tallarines", "espagueti", "fideo", "ramen"}):
        preferred_text_terms.update({"pasta", "espagueti", "tallarin", "fideo", "ramen", "manicotti", "penne", "rotini", "orzo"})

    if wants_side:
        preferred_category_keys.update({"acompanamientos", "salsas y aderezos"})
    if wants_snack:
        preferred_category_keys.update({"bocadillos", "snack"})
    if wants_drink:
        preferred_category_keys.update({"bebidas"})
    if wants_dessert:
        preferred_category_keys.update({"postres"})
    if wants_breakfast:
        preferred_category_keys.update({"desayuno"})
    if wants_lunch:
        preferred_category_keys.update({"almuerzo"})
    if wants_dinner:
        preferred_category_keys.update({"cena"})

    if wants_main_dish and not (wants_side or wants_snack or wants_drink or wants_dessert):
        avoided_category_keys.update({"acompanamientos", "salsas y aderezos", "bocadillos", "bebidas"})
        preferred_category_keys.update({"almuerzo", "cena", "desayuno"})

    return {
        "preferred_category_keys": preferred_category_keys,
        "avoided_category_keys": avoided_category_keys,
        "preferred_text_terms": preferred_text_terms,
        "wants_main_dish": wants_main_dish,
    }


def _assistant_suggestions_from_analysis(
    categories: list[str],
    ingredients: list[str],
    time_limit: int | None,
    message: str = "",
) -> list[str]:
    if _is_exercise_request(message):
        return [
            "Dame una rutina suave de 15 minutos",
            "Ejercicios para empezar en casa",
            "Cómo combinar comida y ejercicio",
            "Hábitos saludables para bajar de peso",
            "Consejos para moverme más durante el día",
        ]

    suggestions: list[str] = []
    if ingredients:
        suggestions.append(f"Dame más ideas con {ingredients[0]} y {ingredients[1] if len(ingredients) > 1 else 'verduras'}")
    if categories:
        suggestions.append(f"Muéstrame otra opción de {categories[0]}")
    if time_limit:
        suggestions.append(f"¿Puedes darme algo aún más rápido que {time_limit} min?")

    defaults = [
        "Quiero 3 recetas para toda la semana",
        "Tengo pollo, arroz y zanahoria, ¿qué preparo?",
        "Opciones altas en proteína",
        "Recetas económicas y saludables",
    ]

    for item in defaults:
        if item not in suggestions:
            suggestions.append(item)

    return suggestions[:5]


def _assistant_recipe_reason(
    recipe: Recipe,
    *,
    categories: list[str],
    ingredients: list[str],
    time_limit: int | None,
    calorie_limit: int | None,
) -> str:
    reasons: list[str] = []
    category_labels = {label.casefold() for label in _categories_for_recipe(recipe) if label}

    matched_categories = [label for label in categories if label.casefold() in category_labels]
    if matched_categories:
        reasons.append(f"entra en {matched_categories[0]}")

    title_text = _fold_text(recipe.title)
    description_text = _fold_text(recipe.description)
    matched_ingredients = [item for item in ingredients if item in title_text or item in description_text]
    if matched_ingredients:
        reasons.append(f"incluye o se acerca a {', '.join(matched_ingredients[:2])}")

    if time_limit and recipe.time_minutes and recipe.time_minutes <= time_limit:
        reasons.append(f"se puede preparar dentro de {time_limit} minutos")

    if calorie_limit and recipe.calories and recipe.calories <= calorie_limit:
        reasons.append(f"se mantiene por debajo de {calorie_limit} kcal")

    if not reasons and recipe.calories:
        reasons.append(f"aporta alrededor de {recipe.calories} kcal por porción")

    if not reasons and recipe.time_minutes:
        reasons.append(f"toma cerca de {recipe.time_minutes} minutos")

    if not reasons:
        category = _category_for_recipe(recipe)
        if category:
            reasons.append(f"es una buena opción de {category.lower()}")
        else:
            reasons.append("encaja como alternativa saludable para variar tu menú")

    return reasons[0]


def _assistant_reply_text(
    *,
    categories: list[str],
    ingredients: list[str],
    time_limit: int | None,
    calorie_limit: int | None,
    profile: Profile | None,
    recipes: list[Recipe],
) -> str:
    filters: list[str] = []
    if categories:
        filters.append(f"algo de tipo {', '.join(categories[:2]).lower()}")
    if ingredients:
        filters.append(f"con {', '.join(ingredients[:3])}")
    if time_limit:
        filters.append(f"que no pase de {time_limit} minutos")
    if calorie_limit:
        filters.append(f"por debajo de {calorie_limit} kcal")

    if filters:
        intro = "Entendí que buscas " + ", ".join(filters) + "."
    else:
        intro = "Revisé tu pedido y busqué opciones del catálogo que puedan encajar contigo."

    if profile and (profile.goal or profile.health_goal):
        goal_text = (profile.goal or profile.health_goal or "").strip()
        intro += f" También tomé en cuenta tu objetivo: {goal_text}."

    if not recipes:
        return intro + " No encontré una coincidencia clara; prueba con otro ingrediente principal, un tipo de comida o un tiempo máximo y te doy opciones más precisas."

    highlighted = recipes[:3]
    recommendations = [
        f"{recipe.title}: {_assistant_recipe_reason(recipe, categories=categories, ingredients=ingredients, time_limit=time_limit, calorie_limit=calorie_limit)}"
        for recipe in highlighted
    ]

    if len(recommendations) == 1:
        closing = f"Mi mejor opción es {recommendations[0]}."
    else:
        closing = "Te recomiendo estas opciones: " + "; ".join(recommendations[:-1]) + f"; y {recommendations[-1]}."

    return intro + " " + closing


def _recipe_to_summary(recipe: Recipe) -> RecipeSummaryResponse:
    return RecipeSummaryResponse(
        id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        description=recipe.description,
        tag=recipe.tag,
        category=_category_for_recipe(recipe),
        calories=recipe.calories,
        time_minutes=recipe.time_minutes,
        image_url=recipe.image_url,
        diet_tags=recipe.diet_tags or [],
    )


_openai_client_cache: dict[str, Any] = {}


def _get_openai_client():
    """Cliente AsyncOpenAI si hay API key; None si no está configurado/instalado."""
    if "client" in _openai_client_cache:
        return _openai_client_cache["client"]
    client = None
    key = (settings.openai_api_key or "").strip()
    if key:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=key)
        except Exception as exc:
            logger.warning("Assistant AI unavailable: could not initialize OpenAI client: %s", exc)
            client = None
    _openai_client_cache["client"] = client
    return client


def _assistant_models() -> list[str]:
    """Modelo principal + fallbacks (del .env), sin duplicados."""
    models: list[str] = []
    primary = (settings.openai_model or "").strip()
    if primary:
        models.append(primary)
    for raw in (settings.openai_fallback_models or "").split(","):
        name = raw.strip()
        if name and name not in models:
            models.append(name)
    return models or ["gpt-4o-mini"]


def _recipes_context(recipes: list[RecipeDetailResponse], *, include_steps: bool = False) -> str:
    lines: list[str] = []
    for recipe in recipes:
        parts = [recipe.title]
        category = recipe.category or recipe.tag
        if category:
            parts.append(f"[{category}]")
        meta: list[str] = []
        if recipe.time_minutes:
            meta.append(f"{recipe.time_minutes} min")
        if recipe.calories:
            meta.append(f"{recipe.calories} kcal")
        if meta:
            parts.append(" · ".join(meta))
        line = "- " + " ".join(parts)

        ingredient_names = [item.name for item in (recipe.ingredients or []) if getattr(item, 'name', None)]
        if ingredient_names:
            line += " | ingredientes: " + ", ".join(ingredient_names[:6])

        if include_steps and recipe.steps:
            step_text = " ".join(
                f"{step.position}. {step.instruction}" for step in recipe.steps[:4] if step.instruction
            )
            if step_text:
                line += " | pasos: " + step_text

        lines.append(line)
    return "\n".join(lines)


async def _generate_assistant_reply(
    *,
    message: str,
    history: list[AssistantHistoryItem],
    recipes: list[RecipeDetailResponse],
    profile: Profile | None,
    
) -> str | None:
    """Respuesta conversacional con OpenAI, anclada a las recetas reales del
    catálogo. Si no hay API key o falla todo, devuelve None."""
    client = _get_openai_client()
    if client is None:
        return None

    recipe_ctx = _recipes_context(recipes, include_steps=_is_preparation_request(message)) or "(sin coincidencias en el catálogo)"
    goal = (profile.goal or profile.health_goal) if profile else None
    has_recipe_context = bool(recipes)
    is_exercise_request = _is_exercise_request(message)
    is_preparation_request = _is_preparation_request(message)

    system = (
        "Eres el asistente de Vimel, una app de recetas saludables. "
        "Respondes en español, cercano y claro, en tono de conversación y sin markdown ni títulos. "
        "Si el usuario está saludando o haciendo una pregunta general, responde de forma breve y natural, "
        "y pregúntale qué quiere cocinar o qué necesita. "
    )
    if has_recipe_context:
        system += (
            "Cuando te pase recetas candidatas, recomienda ÚNICAMENTE recetas de esa lista; no inventes recetas ni datos. "
            "Sé breve (2 a 4 frases): di qué entendiste del pedido y por qué encajan esas recetas. "
            "Las tarjetas de receta se muestran aparte, así que no repitas tiempos ni calorías al detalle."
        )
        if is_preparation_request:
            system += (
                " Si el usuario pide la preparación o los pasos, responde usando los pasos que te comparto de la receta más relevante. "
                "En ese caso sí puedes resumir el proceso de preparación de forma clara y ordenada."
            )
    elif is_exercise_request:
        system += (
            "Si el usuario pide ejercicios o hábitos de actividad física, responde con recomendaciones seguras, simples y realistas "
            "orientadas a una vida saludable. Puedes sugerir caminatas, movilidad, fuerza básica, pausas activas o rutinas cortas, "
            "sin reemplazar consejo médico y sin sugerir esfuerzos extremos. Si hay datos del perfil, úsalos para personalizar."
        )
    else:
        system += (
            "Si no te paso recetas candidatas, no sugieras platos concretos todavía; limita tu respuesta a orientar la conversación."
        )

    user_parts = [f"Mensaje del usuario: {message}"]
    if goal:
        user_parts.append(f"Objetivo del usuario: {goal}")
    if profile and profile.activity_level:
        user_parts.append(f"Nivel de actividad del usuario: {profile.activity_level}")
    if has_recipe_context:
        user_parts.append("Recetas candidatas del catálogo:\n" + recipe_ctx)

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in (history or [])[-6:]:
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:1000]})
    messages.append({"role": "user", "content": "\n\n".join(user_parts)})

    retries = max(0, int(settings.openai_retries or 0))
    delay = max(0, int(settings.openai_retry_delay_ms or 0)) / 1000.0

    for model in _assistant_models():
        for attempt in range(retries + 1):
            try:
                completion = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.5,
                    max_tokens=320,
                )
                text = (completion.choices[0].message.content or "").strip()
                if text:
                    return text
            except Exception as exc:
                logger.warning(
                    "Assistant AI request failed for model %s on attempt %s/%s: %s",
                    model,
                    attempt + 1,
                    retries + 1,
                    exc,
                )
                if attempt < retries and delay:
                    await asyncio.sleep(delay)
                continue
    return None


def _assistant_recipe_details(session: Session, recipes: list[Recipe]) -> list[RecipeDetailResponse]:
    if not recipes:
        return []

    recipe_ids = [int(recipe.id) for recipe in recipes if recipe.id is not None]
    if not recipe_ids:
        return []

    ingredient_rows = session.exec(select(RecipeIngredient).where(RecipeIngredient.recipe_id.in_(recipe_ids))).all()
    step_rows = session.exec(select(RecipeStep).where(RecipeStep.recipe_id.in_(recipe_ids))).all()

    ingredients_by_recipe: dict[int, list[RecipeIngredient]] = defaultdict(list)
    for item in ingredient_rows:
        if item.recipe_id is not None:
            ingredients_by_recipe[int(item.recipe_id)].append(item)

    steps_by_recipe: dict[int, list[RecipeStep]] = defaultdict(list)
    for item in step_rows:
        if item.recipe_id is not None:
            steps_by_recipe[int(item.recipe_id)].append(item)

    return [
        _recipe_to_detail(
            recipe,
            ingredients=ingredients_by_recipe.get(int(recipe.id), []),
            steps=sorted(steps_by_recipe.get(int(recipe.id), []), key=lambda step: step.position or 0),
        )
        for recipe in recipes
        if recipe.id is not None
    ]


@app.post("/api/assistant/chat", response_model=AssistantChatResponse)
async def assistant_chat(
    payload: AssistantChatRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    raw_message = (payload.message or "").strip()
    if not raw_message:
        raise HTTPException(status_code=400, detail="Mensaje vacío")

    user: User | None = None
    profile: Profile | None = None
    try:
        user, _ = _require_user(request, session)
        profile = _get_profile_for_user(session, int(user.id)) if user and user.id else None
    except HTTPException:
        # Permite usar el asistente en modo no autenticado.
        user = None
        profile = None

    message = _fold_text(raw_message)
    time_limit = _extract_time_limit_minutes(message)
    calorie_limit = _detect_calorie_limit(message)

    recipes = session.exec(select(Recipe)).all()
    if not recipes:
        return AssistantChatResponse(
            reply="Aún no hay recetas cargadas. Intenta sembrar datos y vuelve a preguntar.",
            recipes=[],
            suggestions=["Cargar recetas de ejemplo", "Probar con otra búsqueda"],
        )

    all_categories: set[str] = set()
    for recipe in recipes:
        categories = _categories_for_recipe(recipe)
        for label in categories:
            if label:
                all_categories.add(label)

    requested_categories = _detect_requested_categories(message, all_categories)
    ingredient_index = _build_recipe_ingredient_index(session)
    requested_ingredients = _extract_candidate_ingredients(message, ingredient_index)
    search_terms = _assistant_search_terms(message, requested_ingredients)
    is_exercise_request = _is_exercise_request(message)
    show_recipe_cards = False if is_exercise_request else _should_show_recipe_cards(
        message=message,
        categories=requested_categories,
        ingredients=requested_ingredients,
        time_limit=time_limit,
        calorie_limit=calorie_limit,
    )
    intent = _assistant_query_intent(message, search_terms)
    attach_recipe_context = False if is_exercise_request else _should_attach_recipe_context(
        message=message,
        categories=requested_categories,
        ingredients=requested_ingredients,
        time_limit=time_limit,
        calorie_limit=calorie_limit,
    )

    preferred_categories = _preferred_categories_from_profile(profile)
    preferred_keys = {
        _normalize_category_query(label)
        for label in preferred_categories
        if _normalize_category_query(label)
    }

    score_by_recipe: dict[int, int] = Counter()

    for recipe in recipes:
        if recipe.id is None:
            continue
        rid = int(recipe.id)
        score = 0

        recipe_tokens = ingredient_index.get(rid, set())
        for ing in requested_ingredients:
            if ing in recipe_tokens:
                score += 4

        recipe_category_keys = {
            _normalize_category_query(label)
            for label in _categories_for_recipe(recipe)
            if _normalize_category_query(label)
        }

        preferred_intent_keys = intent["preferred_category_keys"]
        avoided_intent_keys = intent["avoided_category_keys"]
        preferred_text_terms = intent["preferred_text_terms"]

        if preferred_intent_keys and preferred_intent_keys.intersection(recipe_category_keys):
            score += 4
        if avoided_intent_keys and avoided_intent_keys.intersection(recipe_category_keys):
            score -= 4

        if requested_categories:
            for req in requested_categories:
                req_key = _normalize_category_query(req)
                if req_key and req_key in recipe_category_keys:
                    score += 3

        if preferred_keys and preferred_keys.intersection(recipe_category_keys):
            score += 2

        if time_limit and recipe.time_minutes and recipe.time_minutes <= time_limit:
            score += 2

        if calorie_limit and recipe.calories and recipe.calories <= calorie_limit:
            score += 1

        title_text = _fold_text(recipe.title)
        desc_text = _fold_text(recipe.description)
        combined_text = " ".join(filter(None, [title_text, desc_text, " ".join(recipe_tokens)]))
        if any(token in title_text or token in desc_text for token in requested_ingredients):
            score += 1

        for term in search_terms:
            if term in title_text:
                score += 5
            elif term in desc_text:
                score += 3
            elif term in combined_text:
                score += 2

        for term in preferred_text_terms:
            if term in title_text:
                score += 2
            elif term in combined_text:
                score += 1

        # Soft boost for recipes with image + concise data.
        if recipe.image_url:
            score += 1

        score_by_recipe[rid] = score

    ranked = sorted(
        [recipe for recipe in recipes if recipe.id is not None],
        key=lambda recipe: (
            score_by_recipe.get(int(recipe.id), 0),
            -int(recipe.time_minutes or 9999),
            -(recipe.calories or 0),
        ),
        reverse=True,
    )

    top_recipes = [recipe for recipe in ranked if score_by_recipe.get(int(recipe.id), 0) > 0][:4]
    if not attach_recipe_context:
        top_recipes = []
    elif not top_recipes:
        text_ranked = sorted(
            [recipe for recipe in recipes if recipe.id is not None],
            key=lambda recipe: (
                sum(
                    1
                    for term in search_terms
                    if term in _fold_text(recipe.title)
                    or term in _fold_text(recipe.description)
                    or term in " ".join(ingredient_index.get(int(recipe.id), set()))
                ),
                1 if recipe.image_url else 0,
            ),
            reverse=True,
        )
        top_recipes = [recipe for recipe in text_ranked if recipe.id is not None][:4]

    top_recipe_details = _assistant_recipe_details(session, top_recipes)

    reply = await _generate_assistant_reply(
        message=raw_message,
        history=payload.history,
        recipes=top_recipe_details,
        profile=profile,
    )

    if not reply:
        reply = (
            "El asistente con IA no está disponible en este momento. "
            "Puedo seguir mostrándote recetas relacionadas, pero la respuesta conversacional depende de la API y ahora mismo no respondió."
        )

    return AssistantChatResponse(
        reply=reply,
        recipes=top_recipe_details if show_recipe_cards else [],
        suggestions=_assistant_suggestions_from_analysis(requested_categories, requested_ingredients, time_limit, raw_message),
    )


def _recipe_to_detail(
    recipe: Recipe,
    *,
    ingredients: list[RecipeIngredient],
    steps: list[RecipeStep],
) -> RecipeDetailResponse:
    summary = _recipe_to_summary(recipe)
    ingredient_payloads = [
        RecipeIngredientPayload(
            name=item.name,
            quantity=item.quantity,
            swap=item.swap,
            optional=item.optional,
        )
        for item in ingredients
    ]
    step_payloads = [
        RecipeStepPayload(position=step.position, instruction=step.instruction)
        for step in steps
    ]
    return RecipeDetailResponse(
        **summary.model_dump(),
        ingredients=ingredient_payloads,
        steps=step_payloads,
    )


def _set_auth_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=bool(settings.cookie_secure),
        samesite=settings.cookie_samesite,
        path="/",
        max_age=settings.jwt_ttl_minutes * 60,
    )


def _clear_auth_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _allowed_redirects() -> list[str]:
    defaults = [settings.frontend_redirect_url]
    cors = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    return list({*defaults, *cors})


def _is_safe_redirect(url: str | None) -> bool:
    if not url:
        return False
    if url.startswith("/"):
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    allowed = _allowed_redirects()
    for origin in allowed:
        try:
            o = urlparse(origin)
        except Exception:
            continue
        if o.scheme and o.netloc and o.scheme == parsed.scheme and o.netloc == parsed.netloc:
            return True
    return False


def _is_allowed_origin(origin: str | None) -> bool:
    """True si `origin` (scheme://host:port) coincide con un origen permitido."""
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    for allowed in _allowed_redirects():
        try:
            o = urlparse(allowed)
        except Exception:
            continue
        if o.scheme == parsed.scheme and o.netloc == parsed.netloc:
            return True
    return False


def _frontend_origin_from_request(request: Request) -> str | None:
    """Origen del frontend que inició el login, tomado de Origin/Referer y validado.

    Mantiene el host consistente (localhost vs 127.0.0.1) entre el inicio del
    login y el redirect final: la cookie de sesión se fija en el host del
    backend que coincide con el frontend, así que el redirect debe volver al
    MISMO host para que esa cookie viaje en las llamadas posteriores del axios.
    """
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        try:
            parsed = urlparse(raw)
        except Exception:
            continue
        if not parsed.scheme or not parsed.netloc:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if _is_allowed_origin(origin):
            return origin
    return None


def _is_mobile_login(flag: str | None) -> bool:
    if not flag:
        return False
    return flag.strip().lower() in {"1", "true", "yes", "mobile"}


def _build_mobile_redirect_target(redirect_path: str | None) -> str:
    base = settings.mobile_frontend_redirect_url.strip() or "com.recetas.saludables://auth/callback"
    if redirect_path and _is_safe_redirect(redirect_path) and redirect_path.startswith("/"):
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urlencode({'redirect': redirect_path})}"
    return base


def _upsert_user(
    session: Session,
    *,
    email: str,
    name: str | None,
    picture: str | None,
    sub: str | None,
) -> User:
    stmt = select(User).where(User.email == email)
    user = session.exec(stmt).first()
    now = datetime.utcnow()

    if user:
        if name:
            user.full_name = name
        if picture:
            user.picture_url = picture
        if sub and not user.google_sub:
            user.google_sub = sub
        user.last_login_at = now
        user.updated_at = now
    else:
        user = User(
            email=email,
            full_name=name,
            picture_url=picture,
            google_sub=sub,
            last_login_at=now,
            updated_at=now,
        )
        session.add(user)

    session.commit()
    session.refresh(user)
    return user


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    rounds = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${rounds}${salt_b64}${digest_b64}"


def _verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, rounds_str, salt_b64, digest_b64 = encoded_hash.split("$", maxsplit=3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_str)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False

    computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(computed, expected)


def _issue_auth_cookie(response: Response, user: User, payload_name: str | None = None) -> None:
    jwt_token = create_access_token(
        subject=user.email,
        payload={
            "email": user.email,
            "name": payload_name or user.full_name,
            "picture": user.picture_url,
            "user_id": user.id,
        },
        secret=settings.jwt_secret,
        ttl_minutes=settings.jwt_ttl_minutes,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        secure=bool(settings.cookie_secure),
        samesite=settings.cookie_samesite,
        path="/",
        max_age=settings.jwt_ttl_minutes * 60,
    )


def _parse_date(raw: str | date | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw

    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _goal_type_from_profile(profile: Profile, initial_weight: float | None, target_weight: float | None) -> str | None:
    goal_text = " ".join(
        str(part or "")
        for part in [profile.health_goal, profile.health_goal_other, profile.goal, profile.goal_message]
    ).lower()

    if any(token in goal_text for token in ["bajar", "perder", "adelgazar", "grasa", "peso"]):
        return "lose"
    if any(token in goal_text for token in ["ganar", "musculo", "músculo", "masa", "fuerza", "volumen"]):
        return "gain"
    if any(token in goal_text for token in ["mantener", "equilibrio", "estable"]):
        return "maintain"
    if initial_weight is not None and target_weight is not None:
        if target_weight < initial_weight:
            return "lose"
        if target_weight > initial_weight:
            return "gain"
        return "maintain"
    return None


def _append_weight_log(session: Session, user_id: int, weight_kg: float | None, *, source: str = "profile") -> None:
    if weight_kg is None:
        return

    latest = session.exec(
        select(UserWeightLog)
        .where(UserWeightLog.user_id == user_id)
        .order_by(UserWeightLog.recorded_at.desc(), UserWeightLog.id.desc())
    ).first()

    if latest and abs(float(latest.weight_kg) - float(weight_kg)) < 0.05:
        return

    session.add(
        UserWeightLog(
            user_id=user_id,
            weight_kg=float(weight_kg),
            recorded_at=datetime.utcnow(),
            source=source,
        )
    )


def _build_goal_progress(session: Session, profile: Profile) -> GoalProgressResponse:
    logs = session.exec(
        select(UserWeightLog)
        .where(UserWeightLog.user_id == profile.user_id)
        .order_by(UserWeightLog.recorded_at.asc(), UserWeightLog.id.asc())
    ).all()

    initial_weight = float(logs[0].weight_kg) if logs else (float(profile.weight_kg) if profile.weight_kg is not None else None)
    latest_log = logs[-1] if logs else None
    current_weight = float(latest_log.weight_kg) if latest_log else (float(profile.weight_kg) if profile.weight_kg is not None else None)
    target_weight = float(profile.target_weight_kg) if profile.target_weight_kg is not None else None
    goal_type = _goal_type_from_profile(profile, initial_weight, target_weight)

    if target_weight is None:
        return GoalProgressResponse(
            status="not_configured",
            label="Meta sin configurar",
            goal_type=goal_type,
            initial_weight_kg=initial_weight,
            current_weight_kg=current_weight,
            target_weight_kg=None,
            remaining_kg=None,
            last_recorded_at=latest_log.recorded_at if latest_log else None,
            target_date=profile.target_date,
            message="Configura un peso meta para saber cuándo se alcanza tu objetivo.",
        )

    if current_weight is None or initial_weight is None:
        return GoalProgressResponse(
            status="needs_more_data",
            label="Faltan mediciones",
            goal_type=goal_type,
            initial_weight_kg=initial_weight,
            current_weight_kg=current_weight,
            target_weight_kg=target_weight,
            remaining_kg=None,
            last_recorded_at=latest_log.recorded_at if latest_log else None,
            target_date=profile.target_date,
            message="Registra tu peso actual para medir el avance hacia tu objetivo.",
        )

    progress_pct = 0.0
    remaining_kg = None
    status = "in_progress"
    label = "En progreso"

    if goal_type == "lose":
        total_delta = max(initial_weight - target_weight, 0.0)
        covered_delta = max(initial_weight - current_weight, 0.0)
        progress_pct = 1.0 if total_delta == 0 else min(max(covered_delta / total_delta, 0.0), 1.0)
        remaining_kg = round(max(current_weight - target_weight, 0.0), 2)
        if current_weight <= target_weight + 0.3:
            status = "achieved"
            label = "Objetivo alcanzado"
    elif goal_type == "gain":
        total_delta = max(target_weight - initial_weight, 0.0)
        covered_delta = max(current_weight - initial_weight, 0.0)
        progress_pct = 1.0 if total_delta == 0 else min(max(covered_delta / total_delta, 0.0), 1.0)
        remaining_kg = round(max(target_weight - current_weight, 0.0), 2)
        if current_weight >= target_weight - 0.3:
            status = "achieved"
            label = "Objetivo alcanzado"
    else:
        diff = abs(current_weight - target_weight)
        remaining_kg = round(diff, 2)
        progress_pct = 1.0 if diff <= 1.0 else max(0.0, 1.0 - min(diff / max(abs(initial_weight - target_weight), 1.0), 1.0))
        if diff <= 1.0:
            status = "achieved"
            label = "Objetivo alcanzado"

    if status == "achieved":
        if goal_type == "maintain":
            message = f"Tu peso actual ({current_weight:.1f} kg) está dentro del rango esperado para mantener tu meta."
        else:
            message = f"Tu peso actual ({current_weight:.1f} kg) ya cumple la meta configurada de {target_weight:.1f} kg."
    else:
        if goal_type == "maintain":
            message = f"Estás a {remaining_kg:.1f} kg de volver al rango esperado de mantenimiento."
        else:
            message = f"Te faltan {remaining_kg:.1f} kg para llegar a tu meta de {target_weight:.1f} kg."

    return GoalProgressResponse(
        status=status,
        label=label,
        goal_type=goal_type,
        progress_pct=round(progress_pct, 4),
        initial_weight_kg=round(initial_weight, 2),
        current_weight_kg=round(current_weight, 2),
        target_weight_kg=round(target_weight, 2),
        remaining_kg=remaining_kg,
        last_recorded_at=latest_log.recorded_at if latest_log else None,
        target_date=profile.target_date,
        message=message,
    )


def _profile_to_response(session: Session, profile: Profile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "birth_date": profile.birth_date.isoformat() if profile.birth_date else None,
        "sex": profile.sex,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "activity_level": profile.activity_level,
        "health_goal": profile.health_goal,
        "health_goal_other": profile.health_goal_other,
        "goal": profile.goal,
        "goal_message": profile.goal_message,
        "target_weight_kg": profile.target_weight_kg,
        "target_date": profile.target_date.isoformat() if profile.target_date else None,
        "motto": profile.motto,
        "food_restrictions": {
            "type": profile.food_restriction_type,
            "items": profile.food_restriction_items or [],
            "other_text": profile.food_restriction_other,
        },
        "goal_progress": _build_goal_progress(session, profile).model_dump(mode="json"),
        "wizard_completed": profile.wizard_completed,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


def _apply_profile_payload(profile: Profile, payload: ProfilePayload, *, partial: bool) -> None:
    data = payload.model_dump(exclude_unset=partial)

    if not partial:
        data = payload.model_dump()

    if "birth_date" in data:
        profile.birth_date = _parse_date(data.get("birth_date"))
    if "sex" in data:
        profile.sex = data.get("sex")
    if "height_cm" in data:
        profile.height_cm = data.get("height_cm")
    if "weight_kg" in data:
        profile.weight_kg = data.get("weight_kg")
    if "activity_level" in data:
        profile.activity_level = data.get("activity_level")
    if "health_goal" in data:
        profile.health_goal = data.get("health_goal")
    if "health_goal_other" in data:
        profile.health_goal_other = data.get("health_goal_other")
    if "goal" in data:
        profile.goal = data.get("goal") or profile.goal or profile.health_goal
    if "goal_message" in data:
        profile.goal_message = data.get("goal_message")
    if "target_weight_kg" in data:
        profile.target_weight_kg = data.get("target_weight_kg")
    if "target_date" in data:
        profile.target_date = _parse_date(data.get("target_date"))
    if "motto" in data:
        profile.motto = data.get("motto")
    if "food_restrictions" in data:
        fr = data.get("food_restrictions") or {}
        profile.food_restriction_type = fr.get("type") or "ninguna"
        profile.food_restriction_items = fr.get("items") or []
        profile.food_restriction_other = fr.get("other_text")


def _get_profile_for_user(session: Session, user_id: int) -> Profile | None:
    stmt = select(Profile).where(Profile.user_id == user_id)
    return session.exec(stmt).first()


def _require_user(request: Request, session: Session) -> tuple[User, dict]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            raw = auth_header.split(" ", maxsplit=1)[1].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="No autenticado")

    try:
        payload = decode_access_token(raw, settings.jwt_secret)
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")

    email = payload.get("email") or payload.get("sub")
    if not email:
        raise HTTPException(status_code=400, detail="No se pudo determinar el usuario")

    user_id = payload.get("user_id")
    user: User | None = None

    if user_id:
        user = session.get(User, user_id)

    if user is None:
        stmt = select(User).where(User.email == email)
        user = session.exec(stmt).first()

    if user is None:
        user = User(email=email, full_name=payload.get("name"), picture_url=payload.get("picture"))
        session.add(user)
        session.commit()
        session.refresh(user)

    return user, payload


@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "Recetas API",
        "docs_url": "/docs",
        "health_url": "/api/health",
        "message": "Backend activo en Render.",
    }


@app.get("/health")
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "Recetas API"}


@app.get("/api/auth/google/login")
async def google_login(
    request: Request,
    redirect: str | None = None,
    origin: str | None = None,
    mobile: str | None = None,
):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth no configurado (GOOGLE_CLIENT_ID/SECRET)")

    if redirect and _is_safe_redirect(redirect):
        request.session["post_login_redirect"] = redirect

    request.session["post_login_mobile"] = _is_mobile_login(mobile)

    # Recordar el origen exacto del frontend (host:puerto) para volver a él al
    # final. El parámetro explícito gana; si no, se infiere de Origin/Referer.
    frontend_origin = origin if _is_allowed_origin(origin) else _frontend_origin_from_request(request)
    if frontend_origin:
        request.session["post_login_origin"] = frontend_origin

    # Usar el mismo host que recibe la petición para no romper la cookie de sesión (state)
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/api/auth/google/callback", name="google_callback")
async def google_callback(request: Request, session: Session = Depends(get_session)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e.error}")

    # Authlib suele proveer userinfo si scope incluye profile/email
    userinfo = token.get("userinfo")
    if userinfo is None:
        # fallback a endpoint userinfo
        userinfo = await oauth.google.userinfo(token=token)

    email = (userinfo or {}).get("email")
    if not email:
        raise HTTPException(status_code=400, detail="No se pudo obtener el email del usuario")

    name = (userinfo or {}).get("name")
    picture = (userinfo or {}).get("picture")
    sub = (userinfo or {}).get("sub") or token.get("userinfo", {}).get("sub")

    user = _upsert_user(
        session,
        email=email,
        name=name,
        picture=picture,
        sub=sub,
    )

    if user.id is not None:
        try:
            ip_address = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")
            session.add(
                UserLoginEvent(
                    user_id=int(user.id),
                    provider="google",
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            user.last_login_at = datetime.utcnow()
            user.updated_at = datetime.utcnow()
            session.add(user)
            session.commit()
        except Exception:
            session.rollback()

    jwt_token = create_access_token(
        subject=email,
        payload={
            "email": email,
            "name": name,
            "picture": picture,
            "user_id": user.id,
        },
        secret=settings.jwt_secret,
        ttl_minutes=settings.jwt_ttl_minutes,
    )

    redirect_path = request.session.pop("post_login_redirect", None)
    if request.session.pop("post_login_mobile", False):
        response = RedirectResponse(url=_build_mobile_redirect_target(redirect_path), status_code=302)
        _set_auth_cookie(response, jwt_token)
        return response

    # Origen del frontend al que volver: el host por el que entró el usuario,
    # para que la cookie de auth (fijada en este host de backend) viaje después.
    frontend_origin = request.session.pop("post_login_origin", None)
    if not _is_allowed_origin(frontend_origin):
        frontend_origin = settings.frontend_redirect_url
    frontend_origin = frontend_origin.rstrip("/")

    if redirect_path and _is_safe_redirect(redirect_path):
        # Ruta relativa -> anclar al origen del frontend; absoluta segura -> usar tal cual.
        redirect_target = f"{frontend_origin}{redirect_path}" if redirect_path.startswith("/") else redirect_path
    else:
        redirect_target = frontend_origin or settings.frontend_redirect_url

    response = RedirectResponse(url=redirect_target, status_code=302)
    _set_auth_cookie(response, jwt_token)
    return response


@app.post("/api/auth/register")
async def auth_register(payload: AuthPasswordPayload, request: Request, response: Response, session: Session = Depends(get_session)):
    email = _normalize_email(payload.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Escribe un correo válido.")

    if len((payload.password or "")) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres.")

    existing_user = session.exec(select(User).where(User.email == email)).first()
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="Este correo ya tiene una cuenta. Inicia sesión.")

    now = datetime.utcnow()
    new_user = User(
        email=email,
        full_name=(payload.full_name or "").strip() or None,
        picture_url=None,
        google_sub=None,
        last_login_at=now,
        updated_at=now,
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    credential = UserCredential(
        user_id=int(new_user.id),
        password_hash=_hash_password(payload.password),
        updated_at=now,
    )
    session.add(credential)
    session.add(
        UserLoginEvent(
            user_id=int(new_user.id),
            provider="password",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )
    session.commit()

    _issue_auth_cookie(response, new_user)
    return {
        "email": new_user.email,
        "name": new_user.full_name,
        "picture": new_user.picture_url,
    }


@app.post("/api/auth/login")
async def auth_login(payload: AuthPasswordPayload, request: Request, response: Response, session: Session = Depends(get_session)):
    email = _normalize_email(payload.email)
    password = payload.password or ""

    if not email or not password:
        raise HTTPException(status_code=400, detail="Escribe tu correo y contraseña.")

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")

    credential = session.get(UserCredential, int(user.id))
    if credential is None:
        raise HTTPException(status_code=401, detail="Esta cuenta se creó con Google. Entra con el botón de Google.")

    if not _verify_password(password, credential.password_hash):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")

    now = datetime.utcnow()
    user.last_login_at = now
    user.updated_at = now
    session.add(user)
    session.add(
        UserLoginEvent(
            user_id=int(user.id),
            provider="password",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )
    session.commit()

    _issue_auth_cookie(response, user)
    return {
        "email": user.email,
        "name": user.full_name,
        "picture": user.picture_url,
    }


@app.post("/api/auth/logout")
async def logout():
    response = RedirectResponse(url=settings.frontend_redirect_url, status_code=302)
    _clear_auth_cookie(response)
    return response


@app.get("/api/auth/me")
async def me(request: Request, session: Session = Depends(get_session)):
    user, payload = _require_user(request, session)
    return {
        "email": user.email,
        "name": user.full_name or payload.get("name"),
        "picture": user.picture_url or payload.get("picture"),
    }


@app.get("/api/auth/me-id")
async def me_id(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    return {"user_id": int(user.id)}


@app.get("/api/profile/status")
async def profile_status(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    profile = _get_profile_for_user(session, user.id)
    has_profile = profile is not None
    login_count = session.exec(
        select(func.count(UserLoginEvent.id)).where(UserLoginEvent.user_id == user.id)
    ).one()
    first_login = bool(login_count <= 1)
    wizard_completed = bool(profile and profile.wizard_completed)
    should_show_wizard = bool(not wizard_completed and first_login)
    return {
        "has_profile": has_profile,
        "wizard_completed": wizard_completed,
        "login_count": int(login_count or 0),
        "first_login": first_login,
        "should_show_wizard": should_show_wizard,
    }


@app.get("/api/profile")
async def get_profile(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    profile = _get_profile_for_user(session, user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    return _profile_to_response(session, profile)


@app.post("/api/profile")
async def create_profile(payload: ProfilePayload, request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    existing = _get_profile_for_user(session, user.id)
    if existing:
        raise HTTPException(status_code=409, detail="El perfil ya existe")

    profile = Profile(user_id=user.id, wizard_completed=True)
    _apply_profile_payload(profile, payload, partial=False)
    profile.updated_at = datetime.utcnow()

    session.add(profile)
    if profile.weight_kg is not None:
        _append_weight_log(session, user.id, profile.weight_kg, source="profile_create")
    session.commit()
    session.refresh(profile)
    return _profile_to_response(session, profile)


@app.put("/api/profile")
async def update_profile(payload: ProfilePayload, request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    profile = _get_profile_for_user(session, user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")

    changed_fields = payload.model_dump(exclude_unset=True)
    _apply_profile_payload(profile, payload, partial=True)
    profile.updated_at = datetime.utcnow()
    session.add(profile)
    if "weight_kg" in changed_fields and profile.weight_kg is not None:
        _append_weight_log(session, user.id, profile.weight_kg, source="profile_update")
    session.commit()
    session.refresh(profile)
    return _profile_to_response(session, profile)


@app.get("/api/profile/goal-progress", response_model=GoalProgressResponse)
async def get_profile_goal_progress(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    profile = _get_profile_for_user(session, user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    return _build_goal_progress(session, profile)


@app.get("/api/recipes", response_model=RecipeListResponse)
async def list_recipes(
    skip: int = 0,
    limit: int = 25,
    search: str | None = None,
    category: str | None = None,
    session: Session = Depends(get_session),
):
    skip = max(skip, 0)
    limit = min(max(limit, 1), 100)

    normalized_search = _fold_text(search) if search and search.strip() else None
    normalized_category = _normalize_category_query(category)

    if normalized_category or normalized_search:
        # Con el volumen actual es viable filtrar en memoria y soportar búsqueda amplia.
        all_recipes = session.exec(select(Recipe)).all()

        ingredients_rows = session.exec(select(RecipeIngredient.recipe_id, RecipeIngredient.name)).all()
        ingredient_text: dict[int, str] = defaultdict(str)
        for rid, name in ingredients_rows:
            if not rid or not name:
                continue
            ingredient_text[int(rid)] += f" {_fold_text(name)}"

        filtered: list[Recipe] = []
        for recipe in all_recipes:
            if normalized_category:
                recipe_categories = _categories_for_recipe(recipe)
                normalized_categories = {
                    _normalize_category_query(label)
                    for label in recipe_categories
                    if _normalize_category_query(label)
                }
                if normalized_category not in normalized_categories:
                    continue

            if normalized_search:
                title = _fold_text(recipe.title)
                desc = _fold_text(recipe.description)
                ing = ingredient_text.get(int(recipe.id or 0), "")
                if normalized_search not in title and normalized_search not in desc and normalized_search not in ing:
                    continue

            filtered.append(recipe)

        filtered.sort(key=lambda r: (r.title or "").lower())
        total = len(filtered)
        recipes = filtered[skip : skip + limit]
    else:
        filters = []
        # sin filtros -> paginación normal

        query = select(Recipe)
        total_stmt = select(func.count()).select_from(Recipe)
        for condition in filters:
            query = query.where(condition)
            total_stmt = total_stmt.where(condition)

        query = query.order_by(Recipe.title).offset(skip).limit(limit)

        recipes = session.exec(query).all()
        total = session.exec(total_stmt).one()

    return RecipeListResponse(
        items=[_recipe_to_summary(recipe) for recipe in recipes],
        total=total,
        skip=skip,
        limit=limit,
    )


@app.get("/api/recipe-categories", response_model=list[RecipeCategoryResponse])
async def list_recipe_categories(session: Session = Depends(get_session)):
    recipes = session.exec(select(Recipe)).all()
    buckets: dict[str, int] = {}
    for recipe in recipes:
        categories = _categories_for_recipe(recipe)
        if not categories:
            categories = [_infer_recipe_category(recipe.title)]
        for label in categories:
            buckets[label] = buckets.get(label, 0) + 1

    result = [RecipeCategoryResponse(label=label, count=count) for label, count in buckets.items()]
    result.sort(key=lambda item: (-item.count, item.label.lower()))
    return result


@app.get("/api/recipes/{slug}", response_model=RecipeDetailResponse)
async def get_recipe(slug: str, session: Session = Depends(get_session)):
    stmt = select(Recipe).where(Recipe.slug == slug)
    recipe = session.exec(stmt).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    ingredients_stmt = (
        select(RecipeIngredient)
        .where(RecipeIngredient.recipe_id == recipe.id)
        .order_by(RecipeIngredient.id)
    )
    steps_stmt = (
        select(RecipeStep)
        .where(RecipeStep.recipe_id == recipe.id)
        .order_by(RecipeStep.position, RecipeStep.id)
    )

    ingredients = session.exec(ingredients_stmt).all()
    steps = session.exec(steps_stmt).all()
    return _recipe_to_detail(recipe, ingredients=ingredients, steps=steps)


@app.post("/nutrition/calculate", response_model=NutritionCalculateResponse)
@app.post("/api/nutrition/calculate", response_model=NutritionCalculateResponse)
async def calculate_recipe_nutrition(payload: NutritionCalculatePayload, session: Session = Depends(get_session)):
    if payload.recipe_id is None and not payload.recipe_slug:
        raise HTTPException(status_code=400, detail="Debes enviar recipe_id o recipe_slug")

    recipe = _resolve_recipe(session, recipe_id=payload.recipe_id, recipe_slug=payload.recipe_slug)
    nutrition = _get_or_create_recipe_nutrition(
        session,
        recipe=recipe,
        force_recalculate=payload.force_recalculate,
    )
    return NutritionCalculateResponse(recipe=_recipe_to_summary(recipe), nutrition=_to_nutrition_response(nutrition))


@app.post("/consumption/register", response_model=ConsumptionRecordResponse)
@app.post("/api/consumption/register", response_model=ConsumptionRecordResponse)
async def register_recipe_consumption(
    payload: ConsumptionRegisterPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    if payload.recipe_id is None and not payload.recipe_slug:
        raise HTTPException(status_code=400, detail="Debes enviar recipe_id o recipe_slug")
    if payload.porcion <= 0:
        raise HTTPException(status_code=400, detail="La porción debe ser mayor a 0")

    recipe = _resolve_recipe(session, recipe_id=payload.recipe_id, recipe_slug=payload.recipe_slug)
    nutrition = _get_or_create_recipe_nutrition(session, recipe=recipe, force_recalculate=False)
    nutrition_for_portion = _to_nutrition_response(nutrition, portion=payload.porcion)

    existing_records = session.exec(
        select(func.count(UserRecipeConsumption.id)).where(UserRecipeConsumption.user_id == user.id)
    ).one()
    existing_baseline_records = session.exec(
        select(func.count(UserRecipeConsumption.id)).where(
            UserRecipeConsumption.user_id == user.id,
            UserRecipeConsumption.is_baseline == True,
        )
    ).one()

    auto_baseline = bool(not payload.baseline and int(existing_baseline_records or 0) == 0 and int(existing_records or 0) < 3)
    consumed_at = payload.fecha_consumo or datetime.utcnow()
    row = UserRecipeConsumption(
        user_id=int(user.id),
        recipe_id=int(recipe.id),
        consumed_at=consumed_at,
        portion=round(payload.porcion, 2),
        nutritional_score=nutrition_for_portion.nutritional_score,
        calories=nutrition_for_portion.calories,
        proteins=nutrition_for_portion.proteins,
        fats=nutrition_for_portion.fats,
        carbohydrates=nutrition_for_portion.carbohydrates,
        sugars=nutrition_for_portion.sugars,
        sodium_mg=nutrition_for_portion.sodium_mg,
        is_baseline=bool(payload.baseline or auto_baseline),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    return ConsumptionRecordResponse(
        id=int(row.id),
        user_id=int(row.user_id),
        recipe_id=int(row.recipe_id),
        fecha_consumo=row.consumed_at,
        porcion=row.portion,
        baseline=row.is_baseline,
        nutrition=nutrition_for_portion,
    )


@app.get("/user/nutrition/summary", response_model=UserNutritionSummaryResponse)
@app.get("/api/user/nutrition/summary", response_model=UserNutritionSummaryResponse)
async def user_nutrition_summary(
    request: Request,
    days: int = 90,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    days = min(max(days, 7), 365)
    from_date = datetime.utcnow() - timedelta(days=days)

    rows = session.exec(
        select(UserRecipeConsumption)
        .where(UserRecipeConsumption.user_id == user.id)
        .where(UserRecipeConsumption.consumed_at >= from_date)
        .where(UserRecipeConsumption.is_baseline == False)
        .order_by(UserRecipeConsumption.consumed_at.asc())
    ).all()

    if not rows:
        return UserNutritionSummaryResponse(
            records=0,
            avg_calories=0.0,
            avg_proteins=0.0,
            avg_nutritional_score=0.0,
            trend=[],
        )

    count = len(rows)
    avg_calories = round(sum(item.calories for item in rows) / count, 2)
    avg_proteins = round(sum(item.proteins for item in rows) / count, 2)
    avg_score = round(sum(item.nutritional_score for item in rows) / count, 2)

    by_week: dict[date, list[UserRecipeConsumption]] = defaultdict(list)
    for item in rows:
        week_key = _week_start(item.consumed_at.date())
        by_week[week_key].append(item)

    trend: list[NutritionTrendPoint] = []
    for week_key in sorted(by_week.keys()):
        bucket = by_week[week_key]
        bucket_count = len(bucket)
        trend.append(
            NutritionTrendPoint(
                period_start=week_key,
                avg_score=round(sum(r.nutritional_score for r in bucket) / bucket_count, 2),
                avg_calories=round(sum(r.calories for r in bucket) / bucket_count, 2),
            )
        )

    return UserNutritionSummaryResponse(
        records=count,
        avg_calories=avg_calories,
        avg_proteins=avg_proteins,
        avg_nutritional_score=avg_score,
        trend=trend,
    )


@app.get("/user/nutrition/comparison", response_model=UserNutritionComparisonResponse)
@app.get("/api/user/nutrition/comparison", response_model=UserNutritionComparisonResponse)
async def user_nutrition_comparison(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)

    rows = session.exec(
        select(UserRecipeConsumption)
        .where(UserRecipeConsumption.user_id == user.id)
        .order_by(UserRecipeConsumption.consumed_at.asc())
    ).all()

    baseline_rows = [row for row in rows if row.is_baseline]
    after_rows = [row for row in rows if not row.is_baseline]

    if not baseline_rows and rows:
        window = min(7, len(rows))
        baseline_rows = rows[:window]
        after_rows = rows[window:]

    baseline_metrics = _comparison_metrics(baseline_rows)
    after_metrics = _comparison_metrics(after_rows)

    return UserNutritionComparisonResponse(
        baseline=baseline_metrics,
        after=after_metrics,
        delta_avg_calories=round(after_metrics.avg_calories - baseline_metrics.avg_calories, 2),
        delta_avg_proteins=round(after_metrics.avg_proteins - baseline_metrics.avg_proteins, 2),
        delta_avg_nutritional_score=round(after_metrics.avg_nutritional_score - baseline_metrics.avg_nutritional_score, 2),
    )


@app.post("/api/consumption/daily-log", response_model=DailyFoodLogResponse)
async def register_daily_food_log(
    payload: DailyFoodLogPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    meal_type = _fold_text(payload.meal_type)
    if meal_type not in _VALID_MEAL_TYPES:
        raise HTTPException(status_code=400, detail="Elige un tipo de comida válido.")
    if not (payload.food_name or "").strip():
        raise HTTPException(status_code=400, detail="Escribe el nombre del alimento.")

    row = DailyFoodLog(
        user_id=int(user.id),
        consumed_at=payload.consumed_at or datetime.utcnow(),
        meal_type=meal_type,
        food_name=payload.food_name.strip(),
        quantity=(payload.quantity or "").strip() or None,
        estimated_cost=payload.estimated_cost,
        calories_estimate=payload.calories_estimate,
        is_healthy=bool(payload.is_healthy),
        notes=(payload.notes or "").strip() or None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    return DailyFoodLogResponse(
        id=int(row.id),
        consumed_at=row.consumed_at,
        meal_type=row.meal_type,
        food_name=row.food_name,
        quantity=row.quantity,
        estimated_cost=row.estimated_cost,
        calories_estimate=row.calories_estimate,
        is_healthy=row.is_healthy,
        notes=row.notes,
    )


@app.get("/api/consumption/daily-log", response_model=list[DailyFoodLogResponse])
async def list_daily_food_logs(
    request: Request,
    days: int = 14,
    limit: int = 200,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    days = min(max(days, 1), 365)
    limit = min(max(limit, 1), 500)
    from_date = datetime.utcnow() - timedelta(days=days)

    rows = session.exec(
        select(DailyFoodLog)
        .where(DailyFoodLog.user_id == int(user.id))
        .where(DailyFoodLog.consumed_at >= from_date)
        .order_by(DailyFoodLog.consumed_at.desc())
        .limit(limit)
    ).all()

    return [
        DailyFoodLogResponse(
            id=int(item.id),
            consumed_at=item.consumed_at,
            meal_type=item.meal_type,
            food_name=item.food_name,
            quantity=item.quantity,
            estimated_cost=item.estimated_cost,
            calories_estimate=item.calories_estimate,
            is_healthy=item.is_healthy,
            notes=item.notes,
        )
        for item in rows
        if item.id is not None
    ]


@app.get("/api/consumption/patterns", response_model=DailyConsumptionSummaryResponse)
async def daily_consumption_patterns(
    request: Request,
    days: int = 30,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    days = min(max(days, 7), 365)
    from_date = datetime.utcnow() - timedelta(days=days)

    rows = session.exec(
        select(DailyFoodLog)
        .where(DailyFoodLog.user_id == int(user.id))
        .where(DailyFoodLog.consumed_at >= from_date)
        .order_by(DailyFoodLog.consumed_at.desc())
    ).all()

    total = len(rows)
    healthy = sum(1 for item in rows if item.is_healthy)
    unhealthy = max(0, total - healthy)

    foods_counter: Counter[str] = Counter()
    unique_days = {item.consumed_at.date() for item in rows}
    for item in rows:
        token = (item.food_name or "").strip()
        if token:
            foods_counter[token] += 1

    return DailyConsumptionSummaryResponse(
        total_logs=total,
        healthy_logs=healthy,
        unhealthy_logs=unhealthy,
        healthy_ratio=round((healthy / total), 3) if total else 0.0,
        avg_daily_logs=round((total / max(1, len(unique_days))), 2) if total else 0.0,
        top_foods=[name for name, _ in foods_counter.most_common(6)],
    )


@app.get("/api/consumption/healthy-frequency", response_model=HealthyFrequencyReportResponse)
async def healthy_frequency_report(
    request: Request,
    days: int = 90,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    days = min(max(days, 14), 365)
    from_date = datetime.utcnow() - timedelta(days=days)

    recipe_rows = session.exec(
        select(UserRecipeConsumption)
        .where(UserRecipeConsumption.user_id == int(user.id))
        .where(UserRecipeConsumption.consumed_at >= from_date)
        .where(UserRecipeConsumption.is_baseline == False)
        .order_by(UserRecipeConsumption.consumed_at.asc())
    ).all()

    manual_rows = session.exec(
        select(DailyFoodLog)
        .where(DailyFoodLog.user_id == int(user.id))
        .where(DailyFoodLog.consumed_at >= from_date)
        .order_by(DailyFoodLog.consumed_at.asc())
    ).all()

    buckets: dict[date, dict[str, int]] = defaultdict(lambda: {"healthy": 0, "unhealthy": 0})
    total_healthy = 0
    total_unhealthy = 0

    for row in recipe_rows:
        key = _week_start(row.consumed_at.date())
        if _is_consumption_healthy(row):
            buckets[key]["healthy"] += 1
            total_healthy += 1
        else:
            buckets[key]["unhealthy"] += 1
            total_unhealthy += 1

    for row in manual_rows:
        key = _week_start(row.consumed_at.date())
        if row.is_healthy:
            buckets[key]["healthy"] += 1
            total_healthy += 1
        else:
            buckets[key]["unhealthy"] += 1
            total_unhealthy += 1

    trend: list[HealthyFrequencyPoint] = []
    for period_start in sorted(buckets.keys()):
        healthy_count = buckets[period_start]["healthy"]
        unhealthy_count = buckets[period_start]["unhealthy"]
        total = healthy_count + unhealthy_count
        trend.append(
            HealthyFrequencyPoint(
                period_start=period_start,
                healthy_count=healthy_count,
                unhealthy_count=unhealthy_count,
                healthy_ratio=round((healthy_count / total), 3) if total else 0.0,
            )
        )

    total_all = total_healthy + total_unhealthy
    return HealthyFrequencyReportResponse(
        days=days,
        total_healthy=total_healthy,
        total_unhealthy=total_unhealthy,
        overall_healthy_ratio=round((total_healthy / total_all), 3) if total_all else 0.0,
        trend=trend,
    )


@app.get("/api/notifications/reminder-settings", response_model=ReminderSettingsResponse)
async def get_reminder_settings(
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    row = _get_or_create_reminder_setting(session, int(user.id))
    target_email = (row.email_override or user.email or "").strip()
    return ReminderSettingsResponse(
        enabled=bool(row.enabled),
        hours_without_log=int(row.hours_without_log),
        email=target_email,
        can_send_email=_can_send_email_reminder() and bool(target_email),
        last_email_sent_at=row.last_email_sent_at,
    )


@app.put("/api/notifications/reminder-settings", response_model=ReminderSettingsResponse)
async def update_reminder_settings(
    payload: ReminderSettingsPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    row = _get_or_create_reminder_setting(session, int(user.id))
    row.enabled = bool(payload.enabled)
    row.hours_without_log = int(payload.hours_without_log)
    row.email_override = (payload.email_override or "").strip() or None
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)

    target_email = (row.email_override or user.email or "").strip()
    return ReminderSettingsResponse(
        enabled=bool(row.enabled),
        hours_without_log=int(row.hours_without_log),
        email=target_email,
        can_send_email=_can_send_email_reminder() and bool(target_email),
        last_email_sent_at=row.last_email_sent_at,
    )


@app.post("/api/notifications/check-reminder", response_model=ReminderCheckResponse)
async def check_reminder_and_notify(
    request: Request,
    force_send: bool = False,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    row = _get_or_create_reminder_setting(session, int(user.id))
    if not row.enabled:
        return ReminderCheckResponse(
            enabled=False,
            should_remind=False,
            hours_since_last_log=0,
            email_sent=False,
            message="Los recordatorios están desactivados.",
        )

    last_log_at = _latest_consumption_datetime(session, int(user.id))
    now = datetime.utcnow()
    if last_log_at is None:
        hours_since = 9999.0
    else:
        hours_since = max(0.0, (now - last_log_at).total_seconds() / 3600.0)

    should_remind = force_send or (hours_since >= float(row.hours_without_log))
    if not should_remind:
        return ReminderCheckResponse(
            enabled=True,
            should_remind=False,
            hours_since_last_log=round(hours_since, 2),
            email_sent=False,
            message="Vas al día con tus registros.",
        )

    target_email = (row.email_override or user.email or "").strip()
    if not target_email:
        return ReminderCheckResponse(
            enabled=True,
            should_remind=True,
            hours_since_last_log=round(hours_since, 2),
            email_sent=False,
            message="Añade un correo para recibir recordatorios.",
        )

    if not _can_send_email_reminder():
        return ReminderCheckResponse(
            enabled=True,
            should_remind=True,
            hours_since_last_log=round(hours_since, 2),
            email_sent=False,
            message="El envío de correos no está disponible ahora.",
        )

    # Evita spam: maximo un correo cada 12 horas, salvo force_send.
    if not force_send and row.last_email_sent_at:
        cooldown_hours = (now - row.last_email_sent_at).total_seconds() / 3600.0
        if cooldown_hours < 12:
            return ReminderCheckResponse(
                enabled=True,
                should_remind=True,
                hours_since_last_log=round(hours_since, 2),
                email_sent=False,
                message="Ya te enviamos un recordatorio hace poco.",
            )

    subject = "Recordatorio Vimel: no olvides registrar tus alimentos"
    body = (
        f"Hola {user.full_name or user.email},\n\n"
        "Detectamos que llevas varias horas sin registrar consumo alimentario en Vimel.\n"
        "Registrar tus comidas ayuda a medir patrones, frecuencia saludable y progreso real.\n\n"
        "Entra a la app y registra tu consumo de hoy.\n\n"
        "Equipo Vimel"
    )

    try:
        _send_email_reminder(to_email=target_email, subject=subject, body_text=body)
    except Exception:
        return ReminderCheckResponse(
            enabled=True,
            should_remind=True,
            hours_since_last_log=round(hours_since, 2),
            email_sent=False,
            message="No se pudo enviar el correo. Revisa la dirección e inténtalo otra vez.",
        )

    row.last_email_sent_at = now
    row.updated_at = now
    session.add(row)
    session.commit()

    return ReminderCheckResponse(
        enabled=True,
        should_remind=True,
        hours_since_last_log=round(hours_since, 2),
        email_sent=True,
        message="Te enviamos el recordatorio por correo.",
    )


@app.post("/api/food-swaps/suggest", response_model=FoodSwapSuggestResponse)
async def suggest_food_swaps(
    payload: FoodSwapSuggestPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    _require_user(request, session)

    candidate_ingredients = list(payload.ingredient_names)
    if payload.recipe_id is not None:
        recipe = session.get(Recipe, payload.recipe_id)
        if recipe is None:
            raise HTTPException(status_code=404, detail="Receta no encontrada")
        recipe_ingredients = session.exec(
            select(RecipeIngredient.name).where(RecipeIngredient.recipe_id == recipe.id)
        ).all()
        candidate_ingredients.extend([name for name in recipe_ingredients if isinstance(name, str)])

    candidates = _extract_swap_candidates(text=payload.query or "", ingredient_names=candidate_ingredients)
    suggestions: list[FoodSwapSuggestion] = []

    for token in candidates:
        rule = _UNHEALTHY_SWAPS.get(token)
        if not rule:
            continue
        suggestions.append(
            FoodSwapSuggestion(
                original=token,
                replacement=rule["swap"],
                reason=rule["reason"],
            )
        )
        if len(suggestions) >= payload.max_suggestions:
            break

    if not suggestions and payload.query:
        fallback = FoodSwapSuggestion(
            original=payload.query.strip(),
            replacement="version al horno o al vapor",
            reason="te ayuda a reducir grasas y sodio sin perder saciedad",
        )
        suggestions = [fallback]

    return FoodSwapSuggestResponse(suggestions=suggestions)


@app.post("/api/food-swaps/register")
async def register_food_swap(
    payload: FoodSwapRegisterPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)

    if not payload.original_food.strip() or not payload.suggested_food.strip():
        raise HTTPException(status_code=400, detail="Indica el alimento original y por cuál lo cambias.")

    row = UserFoodSwapEvent(
        user_id=int(user.id),
        daily_food_log_id=payload.daily_food_log_id,
        original_food=payload.original_food.strip(),
        suggested_food=payload.suggested_food.strip(),
        accepted=bool(payload.accepted),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "ok": True,
        "id": int(row.id),
        "accepted": row.accepted,
    }


@app.get("/api/food-swaps/indicator", response_model=FoodSwapIndicatorResponse)
async def food_swap_indicator(
    request: Request,
    days: int = 90,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    days = min(max(days, 7), 365)
    from_date = datetime.utcnow() - timedelta(days=days)

    rows = session.exec(
        select(UserFoodSwapEvent)
        .where(UserFoodSwapEvent.user_id == int(user.id))
        .where(UserFoodSwapEvent.created_at >= from_date)
    ).all()

    total = len(rows)
    accepted = sum(1 for row in rows if row.accepted)
    ratio = round((accepted / total), 3) if total else 0.0
    return FoodSwapIndicatorResponse(
        total_suggestions=total,
        accepted_suggestions=accepted,
        substitution_rate=ratio,
    )


@app.post("/habits/register-baseline")
@app.post("/api/habits/register-baseline")
async def register_habits_baseline(
    payload: HabitBaselinePayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)

    existing = session.exec(select(UserHabitBaseline).where(UserHabitBaseline.user_id == user.id)).first()
    now = datetime.utcnow()

    raw_score = sum(
        _baseline_component_scores(
            UserHabitBaseline(
                user_id=int(user.id),
                fruit_frequency=payload.fruit_frequency,
                vegetable_frequency=payload.vegetable_frequency,
                junk_food_frequency=payload.junk_food_frequency,
                water_daily_glasses=float(payload.water_daily_glasses),
                meal_schedule=payload.meal_schedule,
            )
        ).values()
    )
    hai = _scale_hai(raw_score)

    if existing is None:
        existing = UserHabitBaseline(
            user_id=int(user.id),
            fruit_frequency=payload.fruit_frequency,
            vegetable_frequency=payload.vegetable_frequency,
            junk_food_frequency=payload.junk_food_frequency,
            water_daily_glasses=float(payload.water_daily_glasses),
            meal_schedule=payload.meal_schedule,
            hai_score=hai,
            created_at=now,
            updated_at=now,
        )
    else:
        existing.fruit_frequency = payload.fruit_frequency
        existing.vegetable_frequency = payload.vegetable_frequency
        existing.junk_food_frequency = payload.junk_food_frequency
        existing.water_daily_glasses = float(payload.water_daily_glasses)
        existing.meal_schedule = payload.meal_schedule
        existing.hai_score = hai
        existing.updated_at = now

    session.add(existing)
    session.commit()
    session.refresh(existing)

    return {
        "ok": True,
        "user_id": int(user.id),
        "baseline_hai": int(existing.hai_score),
        "baseline_level": _hai_level(int(existing.hai_score)).model_dump(),
        "updated_at": existing.updated_at.isoformat(),
    }


@app.post("/habits/register-activity")
@app.post("/api/habits/register-activity")
async def register_habits_activity(
    payload: HabitActivityPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    event_type = _fold_text(payload.event_type)
    allowed = {"app_open", "consumed_recipe", "recipe_selected", "recommendation_interaction"}
    if event_type not in allowed:
        raise HTTPException(status_code=400, detail="event_type no válido")

    row = UserHabitActivity(
        user_id=int(user.id),
        event_type=event_type,
        recipe_id=payload.recipe_id,
        recipe_type=payload.recipe_type,
        interaction_type=payload.interaction_type,
        metadata_json=payload.metadata_json or {},
        created_at=datetime.utcnow(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    return {
        "ok": True,
        "id": int(row.id),
        "user_id": int(row.user_id),
        "event_type": row.event_type,
        "created_at": row.created_at.isoformat(),
    }


@app.get("/habits/user-score/{user_id}", response_model=HabitScoreResponse)
@app.get("/api/habits/user-score/{user_id}", response_model=HabitScoreResponse)
async def get_user_habit_score(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    if int(user.id) != user_id:
        raise HTTPException(status_code=403, detail="No autorizado")

    baseline_hai, after_hai = _compute_habit_scores(session, user_id)
    return HabitScoreResponse(
        user_id=user_id,
        baseline_hai=baseline_hai,
        after_hai=after_hai,
        baseline_level=_hai_level(baseline_hai),
        after_level=_hai_level(after_hai),
    )


@app.get("/habits/user-score/me", response_model=HabitScoreResponse)
@app.get("/api/habits/user-score/me", response_model=HabitScoreResponse)
async def get_my_habit_score(
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    uid = int(user.id)
    baseline_hai, after_hai = _compute_habit_scores(session, uid)
    return HabitScoreResponse(
        user_id=uid,
        baseline_hai=baseline_hai,
        after_hai=after_hai,
        baseline_level=_hai_level(baseline_hai),
        after_level=_hai_level(after_hai),
    )


@app.get("/habits/comparison/{user_id}", response_model=HabitComparisonResponse)
@app.get("/api/habits/comparison/{user_id}", response_model=HabitComparisonResponse)
async def get_habit_comparison(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    if int(user.id) != user_id:
        raise HTTPException(status_code=403, detail="No autorizado")

    baseline_hai, after_hai = _compute_habit_scores(session, user_id)
    improvement = round(after_hai - baseline_hai, 2)
    return HabitComparisonResponse(
        user_id=user_id,
        baseline_avg_hai=float(baseline_hai),
        after_avg_hai=float(after_hai),
        improvement=improvement,
    )


@app.get("/habits/comparison/me", response_model=HabitComparisonResponse)
@app.get("/api/habits/comparison/me", response_model=HabitComparisonResponse)
async def get_my_habit_comparison(
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    uid = int(user.id)
    baseline_hai, after_hai = _compute_habit_scores(session, uid)
    return HabitComparisonResponse(
        user_id=uid,
        baseline_avg_hai=float(baseline_hai),
        after_avg_hai=float(after_hai),
        improvement=round(after_hai - baseline_hai, 2),
    )


@app.get("/habits/global-report", response_model=HabitGlobalReportResponse)
@app.get("/api/habits/global-report", response_model=HabitGlobalReportResponse)
async def get_habits_global_report(
    request: Request,
    session: Session = Depends(get_session),
):
    _require_user(request, session)

    baselines = session.exec(select(UserHabitBaseline)).all()
    activities_user_ids = session.exec(select(UserHabitActivity.user_id).distinct()).all()
    activity_users = {int(uid) for uid in activities_user_ids if uid is not None}

    baseline_scores: list[int] = []
    after_scores: list[int] = []
    improvements: list[float] = []

    for row in baselines:
        uid = int(row.user_id)
        baseline_hai, after_hai = _compute_habit_scores(session, uid)
        baseline_scores.append(baseline_hai)
        after_scores.append(after_hai)
        improvements.append(float(after_hai - baseline_hai))

    def _avg(values: list[float] | list[int]) -> float:
        if not values:
            return 0.0
        return round(float(sum(values)) / float(len(values)), 2)

    return HabitGlobalReportResponse(
        total_users_with_baseline=len(baselines),
        total_users_with_activity=len(activity_users),
        average_baseline_hai=_avg(baseline_scores),
        average_after_hai=_avg(after_scores),
        average_improvement=_avg(improvements),
    )


@app.get("/habits/activity-history/{user_id}", response_model=HabitActivityHistoryResponse)
@app.get("/api/habits/activity-history/{user_id}", response_model=HabitActivityHistoryResponse)
async def get_habit_activity_history(
    user_id: int,
    request: Request,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    if int(user.id) != user_id:
        raise HTTPException(status_code=403, detail="No autorizado")

    safe_limit = min(max(int(limit), 1), 300)
    rows = session.exec(
        select(UserHabitActivity)
        .where(UserHabitActivity.user_id == user_id)
        .order_by(UserHabitActivity.created_at.desc())
        .limit(safe_limit)
    ).all()

    return HabitActivityHistoryResponse(
        user_id=user_id,
        items=[
            HabitActivityItemResponse(
                id=int(item.id),
                event_type=item.event_type,
                recipe_id=item.recipe_id,
                recipe_type=item.recipe_type,
                interaction_type=item.interaction_type,
                created_at=item.created_at,
            )
            for item in rows
            if item.id is not None
        ],
    )


@app.get("/habits/activity-history/me", response_model=HabitActivityHistoryResponse)
@app.get("/api/habits/activity-history/me", response_model=HabitActivityHistoryResponse)
async def get_my_habit_activity_history(
    request: Request,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    uid = int(user.id)
    safe_limit = min(max(int(limit), 1), 300)
    rows = session.exec(
        select(UserHabitActivity)
        .where(UserHabitActivity.user_id == uid)
        .order_by(UserHabitActivity.created_at.desc())
        .limit(safe_limit)
    ).all()
    return HabitActivityHistoryResponse(
        user_id=uid,
        items=[
            HabitActivityItemResponse(
                id=int(item.id),
                event_type=item.event_type,
                recipe_id=item.recipe_id,
                recipe_type=item.recipe_type,
                interaction_type=item.interaction_type,
                created_at=item.created_at,
            )
            for item in rows
            if item.id is not None
        ],
    )


@app.get("/api/favorites", response_model=FavoriteListResponse)
async def list_favorites(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)

    stmt = (
        select(Recipe)
        .join(UserFavoriteRecipe, UserFavoriteRecipe.recipe_id == Recipe.id)
        .where(UserFavoriteRecipe.user_id == user.id)
        .order_by(UserFavoriteRecipe.created_at.desc(), Recipe.title)
    )
    recipes = session.exec(stmt).all()
    return FavoriteListResponse(items=[_recipe_to_summary(recipe) for recipe in recipes])


@app.post("/api/favorites/{slug}")
async def add_favorite(slug: str, request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)

    recipe = session.exec(select(Recipe).where(Recipe.slug == slug)).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    existing = session.get(UserFavoriteRecipe, (user.id, recipe.id))
    if existing is None:
        session.add(UserFavoriteRecipe(user_id=user.id, recipe_id=int(recipe.id)))
        session.commit()
    return {"ok": True}


@app.delete("/api/favorites/{slug}")
async def remove_favorite(slug: str, request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)

    recipe = session.exec(select(Recipe).where(Recipe.slug == slug)).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    existing = session.get(UserFavoriteRecipe, (user.id, recipe.id))
    if existing is not None:
        session.delete(existing)
        session.commit()
    return {"ok": True}


@app.get("/api/recents", response_model=RecentListResponse)
async def list_recents(
    request: Request,
    limit: int = 12,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    limit = min(max(limit, 1), 50)

    stmt = (
        select(UserRecentRecipe, Recipe)
        .join(Recipe, Recipe.id == UserRecentRecipe.recipe_id)
        .where(UserRecentRecipe.user_id == user.id)
        .order_by(UserRecentRecipe.seen_at.desc())
        .limit(limit)
    )

    rows = session.exec(stmt).all()
    items: list[RecentRecipeResponse] = []
    for recent_row, recipe in rows:
        summary = _recipe_to_summary(recipe)
        items.append(RecentRecipeResponse(**summary.model_dump(), seen_at=recent_row.seen_at))

    return RecentListResponse(items=items)


@app.post("/api/recents/{slug}")
async def add_recent(slug: str, request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)

    recipe = session.exec(select(Recipe).where(Recipe.slug == slug)).first()
    if not recipe:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    # Mantener un solo registro por receta/usuario.
    session.exec(
        delete(UserRecentRecipe).where(
            UserRecentRecipe.user_id == user.id,
            UserRecentRecipe.recipe_id == int(recipe.id),
        )
    )
    session.add(UserRecentRecipe(user_id=user.id, recipe_id=int(recipe.id)))
    session.commit()

    # Limitar historial a los últimos 12.
    keep = 12
    ids_stmt = (
        select(UserRecentRecipe.id)
        .where(UserRecentRecipe.user_id == user.id)
        .order_by(UserRecentRecipe.seen_at.desc())
    )
    ids = [int(rid) for rid in session.exec(ids_stmt).all() if rid is not None]
    if len(ids) > keep:
        to_delete = ids[keep:]
        session.exec(delete(UserRecentRecipe).where(UserRecentRecipe.id.in_(to_delete)))
        session.commit()

    return {"ok": True}


@app.delete("/api/recents")
async def clear_recents(request: Request, session: Session = Depends(get_session)):
    user, _ = _require_user(request, session)
    session.exec(delete(UserRecentRecipe).where(UserRecentRecipe.user_id == user.id))
    session.commit()
    return {"ok": True}


@app.get("/api/weekly-plan", response_model=WeeklyPlanResponse)
async def get_weekly_plan(
    request: Request,
    week_start: date | None = None,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

    start = _week_start(week_start or date.today())
    plan = _get_weekly_plan(session, user_id=int(user.id), week_start_date=start)
    if plan is None:
        # Plan vacío (el frontend puede llamar /generate para llenarlo)
        plan = WeeklyPlan(user_id=int(user.id), week_start_date=start, theme="")
        session.add(plan)
        session.commit()
        session.refresh(plan)

    return _weekly_plan_to_response(session, plan, day_names=day_names)


@app.post("/api/weekly-plan/generate", response_model=WeeklyPlanResponse)
async def generate_weekly_plan(
    request: Request,
    week_start: date | None = None,
    weekly_budget: float | None = None,
    health_focus: str | None = None,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    start = _week_start(week_start or date.today())
    plan_response, _ = _build_generated_weekly_plan(
        session,
        user_id=int(user.id),
        week_start_date=start,
        include_snacks=False,
        weekly_budget=weekly_budget,
        health_focus=health_focus,
    )
    return plan_response


@app.post("/api/weekly-plan/generate-smart", response_model=SmartWeeklyPlanResponse)
async def generate_smart_weekly_plan(
    payload: PlanGenerationPayload,
    request: Request,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    start = _week_start(payload.week_start or date.today())
    plan_response, estimated_total = _build_generated_weekly_plan(
        session,
        user_id=int(user.id),
        week_start_date=start,
        include_snacks=bool(payload.include_snacks),
        weekly_budget=payload.weekly_budget,
        health_focus=payload.health_focus,
    )

    within_budget = True
    if payload.weekly_budget is not None:
        within_budget = bool(estimated_total <= payload.weekly_budget)

    return SmartWeeklyPlanResponse(
        plan=plan_response,
        budget=PlanBudgetResponse(
            estimated_total_cost=estimated_total,
            weekly_budget=payload.weekly_budget,
            within_budget=within_budget,
        ),
    )


# Unidades de medida frecuentes en el dataset (para separar cantidad de ingrediente)
_SHOPPING_UNITS = {
    "cucharadita", "cucharaditas", "cucharada", "cucharadas", "taza", "tazas",
    "libra", "libras", "onza", "onzas", "lata", "latas", "diente", "dientes",
    "g", "gr", "gramo", "gramos", "kg", "ml", "l", "litro", "litros", "pizca",
    "pizcas", "puñado", "punado", "cuarto", "cuartos", "paquete", "paquetes",
    "manojo", "manojos", "rebanada", "rebanadas", "tira", "tiras", "spray",
    "ramita", "ramitas", "barra", "barras", "frasco", "tallo", "tallos",
}

# Pasillos del supermercado (orden = prioridad de coincidencia)
_SHOPPING_AISLES = [
    ("Carnes y pescados", ["pollo", "carne", "res", "cerdo", "pavo", "pescado", "atun", "salmon", "camaron", "tocino", "jamon", "salchicha", "salchichon", "kielbasa", "chorizo", "filete", "molida"]),
    ("Lácteos y huevos", ["leche", "queso", "yogur", "mantequilla", "margarina", "huevo", "crema", "nata"]),
    ("Especias y condimentos", ["sal", "pimienta", "comino", "oregano", "canela", "azucar", "vainilla", "ajo en polvo", "chile en polvo", "chile rojo", "curry", "paprika", "nuez moscada", "laurel", "vinagre", "salsa de soya", "aceite", "especia", "condimento", "mostaza", "mayonesa", "ketchup", "catsup", "sazon", "soya", "albahaca", "tomillo", "romero", "eneldo", "perejil", "extracto"]),
    ("Panadería y granos", ["pan", "tortilla", "arroz", "pasta", "fideo", "espagueti", "harina", "avena", "cereal", "quinoa", "maicena", "migajas", "galleta", "bicarbonato", "levadura", "hornear", "wantan"]),
    ("Despensa", ["frijol", "lenteja", "garbanzo", "lata", "caldo", "salsa", "aceituna", "miel", "mermelada", "nuez", "nueces", "almendra", "pacana", "semilla", "coco", "chocolate", "cacao", "tomate en", "azucar morena", "higo", "deshidratado"]),
    ("Verduras y frutas", ["cebolla", "cebollin", "ajo", "tomate", "zanahoria", "apio", "calabaza", "calabacita", "brocoli", "espinaca", "lechuga", "pimiento", "papa", "patata", "camote", "fruta", "verdura", "vegetal", "manzana", "platano", "banana", "limon", "cilantro", "perejil", "champinon", "pepino", "aguacate", "palta", "maiz", "elote", "chile", "chicharo", "arveja", "jalapeno", "col", "repollo", "fresa", "arandano", "pina", "mango", "durazno", "uva", "naranja", "jengibre", "bayas"]),
]


# Adjetivos/estados que no cambian QUÉ se compra (para consolidar variantes)
_SHOPPING_DESCRIPTORS = {
    "picado", "picada", "picados", "picadas", "fresco", "fresca", "frescos",
    "frescas", "molido", "molida", "molidos", "molidas", "rallado", "rallada",
    "seco", "seca", "secos", "secas", "grande", "grandes", "mediano", "mediana",
    "medianas", "medianos", "pequeno", "pequena", "pequenos", "pequenas",
    "batido", "batida", "cocido", "cocida", "finamente", "muy", "entera",
    "entero", "tostado", "tostada", "tostadas", "tostados", "descongelado",
    "descongelada", "crudo", "cruda", "pelado", "pelada", "peladas", "pelados",
    "cortado", "cortada", "cortadas", "natural", "frio", "fria", "caliente",
}
# Conectores que abren una alternativa ("arroz blanco o integral") o cola
_SHOPPING_STOP = {"o", "u", "y", "e"}
_SHOPPING_TRAIL = {"de", "en", "con", "para", "del", "la", "el", "su", "al", "a", "sin", "tipo"}


def _shopping_base_ingredient(raw: str) -> tuple[str, str]:
    """Devuelve (ingrediente_base, prefijo_cantidad) a partir de un texto libre
    como '1/4 cucharadita de sal' -> ('sal', '1/4 cucharadita')."""
    s = (raw or "").strip()
    s = re.sub(r"\([^)]*\)", " ", s)          # quitar paréntesis
    s = s.split(",")[0]                          # quitar ", picado"/", pelado"
    s = re.sub(r"\bopcional\b", " ", s, flags=re.IGNORECASE)

    qty_tokens: list[str] = []
    rest: list[str] = []
    skipping = True
    for tok in s.split():
        low = _fold_text(tok).strip(".")
        if skipping and (
            re.fullmatch(r"[\d/.,½¼¾⅓⅔–-]+", low)
            or low in _SHOPPING_UNITS
            or low in {"de", "o", "y", "del", "la", "el"}
        ):
            if low not in {"de", "o", "y", "del", "la", "el"}:
                qty_tokens.append(tok)
            continue
        skipping = False
        if low in _SHOPPING_STOP:        # alternativa: nos quedamos con lo anterior
            break
        if low in _SHOPPING_DESCRIPTORS:  # adjetivo: lo omitimos
            continue
        if re.fullmatch(r"[\d/.,½¼¾⅓⅔–-]+", low) or len(low) <= 1:
            continue                       # números sueltos o letras: ruido
        rest.append(tok)

    rest = rest[:3]
    while rest and _fold_text(rest[-1]) in _SHOPPING_TRAIL:
        rest.pop()
    base = " ".join(rest).strip(" .-")
    return (base or s.strip() or raw.strip(), " ".join(qty_tokens).strip())


def _word_matches(word: str, kw: str) -> bool:
    # Coincidencia tolerante a plural: huevo/huevos, almendra/almendras.
    return word == kw or word == kw + "s" or word == kw + "es"


def _shopping_aisle(base: str) -> str:
    folded = _fold_text(base)
    words = folded.split()
    for aisle, keywords in _SHOPPING_AISLES:
        for kw in keywords:
            if " " in kw:
                if kw in folded:
                    return aisle
            elif any(_word_matches(w, kw) for w in words):
                return aisle
    return "Otros"


@app.get("/api/weekly-plan/shopping-list", response_model=ShoppingListResponse)
async def weekly_plan_shopping_list(
    request: Request,
    week_start: date | None = None,
    session: Session = Depends(get_session),
):
    user, _ = _require_user(request, session)
    start = _week_start(week_start or date.today())
    plan = _get_weekly_plan(session, user_id=int(user.id), week_start_date=start)
    if plan is None:
        return ShoppingListResponse(items=[])

    meals = session.exec(select(WeeklyMeal).where(WeeklyMeal.weekly_plan_id == plan.id)).all()
    recipe_ids = [int(m.recipe_id) for m in meals if m.recipe_id]
    if not recipe_ids:
        return ShoppingListResponse(items=[])

    recipes = session.exec(select(Recipe).where(Recipe.id.in_(recipe_ids))).all()
    recipe_by_id = {int(r.id): r for r in recipes if r.id is not None}

    ingredients = session.exec(select(RecipeIngredient).where(RecipeIngredient.recipe_id.in_(recipe_ids))).all()

    buckets: dict[str, dict[str, object]] = {}
    for ing in ingredients:
        name = (ing.name or "").strip()
        if not name:
            continue

        base, qty_prefix = _shopping_base_ingredient(name)
        key = _fold_text(base)
        if not key:
            continue

        if key not in buckets:
            buckets[key] = {
                "name": base[:1].upper() + base[1:] if base else base,
                "count": 0,
                "quantities": [],
                "recipes": set(),
                "category": _shopping_aisle(base),
            }

        buckets[key]["count"] = int(buckets[key]["count"]) + 1
        # Cantidad: la del campo quantity si existe, si no el prefijo extraído.
        qty = (ing.quantity or "").strip() or qty_prefix
        if qty:
            buckets[key]["quantities"].append(qty)

        recipe_title = None
        if ing.recipe_id and int(ing.recipe_id) in recipe_by_id:
            recipe_title = recipe_by_id[int(ing.recipe_id)].title
        if recipe_title:
            buckets[key]["recipes"].add(recipe_title)

    items: list[ShoppingListItemResponse] = []
    for _, data in buckets.items():
        quantities = list(dict.fromkeys([q for q in data["quantities"] if q]))
        recipes_for_item = sorted(list(data["recipes"]))
        items.append(
            ShoppingListItemResponse(
                name=str(data["name"]),
                count=int(data["count"]),
                quantities=quantities,
                recipes=recipes_for_item,
                category=str(data["category"]),
            )
        )

    # Orden por pasillo (según _SHOPPING_AISLES) y luego alfabético.
    aisle_order = {aisle: i for i, (aisle, _) in enumerate(_SHOPPING_AISLES)}
    aisle_order["Otros"] = len(aisle_order)
    items.sort(key=lambda item: (aisle_order.get(item.category, 99), item.name.lower()))
    return ShoppingListResponse(items=items)
