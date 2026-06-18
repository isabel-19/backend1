from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Column, JSON, String
from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(sa_column=Column("email", String(255), unique=True, index=True, nullable=False))
    full_name: str | None = Field(default=None, sa_column=Column("full_name", String(255)))
    picture_url: str | None = Field(default=None, sa_column=Column("picture_url", String(1024)))
    google_sub: str | None = Field(default=None, sa_column=Column("google_sub", String(255), unique=True, nullable=True))
    last_login_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserCredential(SQLModel, table=True):
    __tablename__ = "user_credentials"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    password_hash: str = Field(sa_column=Column("password_hash", String(512), nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserLoginEvent(SQLModel, table=True):
    __tablename__ = "user_login_events"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str | None = Field(default=None, sa_column=Column("provider", String(32)))
    ip_address: str | None = Field(default=None, sa_column=Column("ip_address", String(64)))
    user_agent: str | None = Field(default=None, sa_column=Column("user_agent", String(512)))
    logged_in_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", unique=True, nullable=False, index=True)
    birth_date: date | None = None
    sex: str | None = Field(default=None, sa_column=Column("sex", String(32)))
    height_cm: float | None = None
    weight_kg: float | None = None
    activity_level: str | None = Field(default=None, sa_column=Column("activity_level", String(64)))
    health_goal: str | None = Field(default=None, sa_column=Column("health_goal", String(64)))
    health_goal_other: str | None = Field(default=None, sa_column=Column("health_goal_other", String(255)))
    goal: str | None = Field(default=None, sa_column=Column("goal", String(255)))
    goal_message: str | None = Field(default=None, sa_column=Column("goal_message", String(512)))
    target_weight_kg: float | None = None
    target_date: date | None = None
    motto: str | None = Field(default=None, sa_column=Column("motto", String(255)))
    food_restriction_type: str = Field(
        default="ninguna",
        sa_column=Column("food_restriction_type", String(64), nullable=False, default="ninguna"),
    )
    food_restriction_items: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, default=list),
    )
    food_restriction_other: str | None = Field(default=None, sa_column=Column("food_restriction_other", String(255)))
    wizard_completed: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class Recipe(SQLModel, table=True):
    __tablename__ = "recipes"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(sa_column=Column("slug", String(128), unique=True, index=True, nullable=False))
    title: str = Field(sa_column=Column("title", String(255), nullable=False))
    description: str | None = Field(default=None, sa_column=Column("description", String(1024)))
    tag: str | None = Field(default=None, sa_column=Column("tag", String(64)))
    calories: int | None = None
    time_minutes: int | None = None
    image_url: str | None = Field(default=None, sa_column=Column("image_url", String(1024)))
    diet_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    created_by_id: int | None = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class RecipeIngredient(SQLModel, table=True):
    __tablename__ = "recipe_ingredients"

    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipes.id", index=True, nullable=False)
    name: str = Field(sa_column=Column("name", String(255), nullable=False))
    quantity: str | None = Field(default=None, sa_column=Column("quantity", String(255)))
    swap: str | None = Field(default=None, sa_column=Column("swap", String(255)))
    optional: bool = Field(default=False, nullable=False)


class RecipeStep(SQLModel, table=True):
    __tablename__ = "recipe_steps"

    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipes.id", index=True, nullable=False)
    position: int = Field(default=1, nullable=False)
    instruction: str = Field(sa_column=Column("instruction", String(1024), nullable=False))


class RecipeMacro(SQLModel, table=True):
    __tablename__ = "recipe_macros"

    recipe_id: int = Field(foreign_key="recipes.id", primary_key=True)
    protein_g: float | None = None
    carbs_g: float | None = None
    fats_g: float | None = None


class RecipeNutrition(SQLModel, table=True):
    __tablename__ = "recipe_nutrition"

    recipe_id: int = Field(foreign_key="recipes.id", primary_key=True)
    calories: float = Field(default=0, nullable=False)
    proteins: float = Field(default=0, nullable=False)
    fats: float = Field(default=0, nullable=False)
    carbohydrates: float = Field(default=0, nullable=False)
    sugars: float = Field(default=0, nullable=False)
    sodium_mg: float = Field(default=0, nullable=False)
    source: str = Field(default="internal-db", sa_column=Column("source", String(64), nullable=False, default="internal-db"))
    calculated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserRecipeConsumption(SQLModel, table=True):
    __tablename__ = "user_recipe_consumption"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    recipe_id: int = Field(foreign_key="recipes.id", nullable=False, index=True)
    consumed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False, index=True)
    portion: float = Field(default=1.0, nullable=False)
    nutritional_score: float = Field(default=0, nullable=False)
    calories: float = Field(default=0, nullable=False)
    proteins: float = Field(default=0, nullable=False)
    fats: float = Field(default=0, nullable=False)
    carbohydrates: float = Field(default=0, nullable=False)
    sugars: float = Field(default=0, nullable=False)
    sodium_mg: float = Field(default=0, nullable=False)
    is_baseline: bool = Field(default=False, nullable=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserHabitBaseline(SQLModel, table=True):
    __tablename__ = "user_habit_baselines"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", unique=True, nullable=False, index=True)
    fruit_frequency: str = Field(sa_column=Column("fruit_frequency", String(32), nullable=False))
    vegetable_frequency: str = Field(sa_column=Column("vegetable_frequency", String(32), nullable=False))
    junk_food_frequency: str = Field(sa_column=Column("junk_food_frequency", String(32), nullable=False))
    water_daily_glasses: float = Field(default=0, nullable=False)
    meal_schedule: str | None = Field(default=None, sa_column=Column("meal_schedule", String(255)))
    hai_score: int = Field(default=0, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserWeightLog(SQLModel, table=True):
    __tablename__ = "user_weight_logs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    weight_kg: float = Field(nullable=False)
    recorded_at: datetime = Field(default_factory=datetime.utcnow, nullable=False, index=True)
    source: str = Field(default="profile", sa_column=Column("source", String(64), nullable=False, default="profile"))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserHabitActivity(SQLModel, table=True):
    __tablename__ = "user_habit_activities"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    event_type: str = Field(sa_column=Column("event_type", String(48), nullable=False, index=True))
    recipe_id: int | None = Field(default=None, foreign_key="recipes.id")
    recipe_type: str | None = Field(default=None, sa_column=Column("recipe_type", String(64)))
    interaction_type: str | None = Field(default=None, sa_column=Column("interaction_type", String(64)))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False, index=True)


class UserFavoriteRecipe(SQLModel, table=True):
    __tablename__ = "user_favorite_recipes"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    recipe_id: int = Field(foreign_key="recipes.id", primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserRecentRecipe(SQLModel, table=True):
    __tablename__ = "user_recent_recipes"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    recipe_id: int = Field(foreign_key="recipes.id", nullable=False)
    seen_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class PantryItem(SQLModel, table=True):
    __tablename__ = "pantry_items"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    name: str = Field(sa_column=Column("name", String(255), nullable=False))
    is_missing: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class WeeklyPlan(SQLModel, table=True):
    __tablename__ = "weekly_plans"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    week_start_date: date = Field(nullable=False)
    theme: str | None = Field(default=None, sa_column=Column("theme", String(255)))
    notes: str | None = Field(default=None, sa_column=Column("notes", String(1024)))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class WeeklyMeal(SQLModel, table=True):
    __tablename__ = "weekly_meals"

    id: int | None = Field(default=None, primary_key=True)
    weekly_plan_id: int = Field(foreign_key="weekly_plans.id", nullable=False, index=True)
    day_of_week: str = Field(sa_column=Column("day_of_week", String(16), nullable=False))
    meal_type: str = Field(sa_column=Column("meal_type", String(32), nullable=False))
    recipe_id: int | None = Field(default=None, foreign_key="recipes.id")
    custom_label: str | None = Field(default=None, sa_column=Column("custom_label", String(255)))
    notes: str | None = Field(default=None, sa_column=Column("notes", String(512)))


class DailyFoodLog(SQLModel, table=True):
    __tablename__ = "daily_food_logs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    consumed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False, index=True)
    meal_type: str = Field(default="snack", sa_column=Column("meal_type", String(32), nullable=False, default="snack"))
    food_name: str = Field(sa_column=Column("food_name", String(255), nullable=False))
    quantity: str | None = Field(default=None, sa_column=Column("quantity", String(128)))
    estimated_cost: float | None = None
    calories_estimate: float | None = None
    is_healthy: bool = Field(default=False, nullable=False, index=True)
    notes: str | None = Field(default=None, sa_column=Column("notes", String(512)))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class UserFoodSwapEvent(SQLModel, table=True):
    __tablename__ = "user_food_swap_events"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    daily_food_log_id: int | None = Field(default=None, foreign_key="daily_food_logs.id")
    original_food: str = Field(sa_column=Column("original_food", String(255), nullable=False))
    suggested_food: str = Field(sa_column=Column("suggested_food", String(255), nullable=False))
    accepted: bool = Field(default=False, nullable=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False, index=True)


class UserReminderSetting(SQLModel, table=True):
    __tablename__ = "user_reminder_settings"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", unique=True, nullable=False, index=True)
    enabled: bool = Field(default=False, nullable=False)
    hours_without_log: int = Field(default=24, nullable=False)
    email_override: str | None = Field(default=None, sa_column=Column("email_override", String(255)))
    last_email_sent_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
