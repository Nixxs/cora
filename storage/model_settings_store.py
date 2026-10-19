"""Save which model each area of Cora uses; an area with no saved choice uses the default."""

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from session_config import BACKEND_MODEL, MODEL_AREAS, MODEL_CHOICES
from .models import ModelSettingRecord


class ModelSettingsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def models(self) -> dict[str, str]:
        """Return every area's model; a saved model no longer in the list falls back to the default."""
        with Session(self.engine) as session:
            saved = dict(session.execute(select(ModelSettingRecord.area, ModelSettingRecord.model)).all())
        return {
            area: saved[area] if saved.get(area) in MODEL_CHOICES else BACKEND_MODEL
            for area in MODEL_AREAS
        }

    def model_for(self, area: str) -> str:
        """Return the model one area should use now."""
        return self.models()[area]

    def set_model(self, area: str, model: str) -> None:
        """Save one area's model, replacing any earlier choice."""
        if area not in MODEL_AREAS:
            raise ValueError(f"Unknown model area: {area}")
        if model not in MODEL_CHOICES:
            raise ValueError(f"Unknown model: {model}")
        statement = insert(ModelSettingRecord).values(area=area, model=model)
        statement = statement.on_conflict_do_update(
            index_elements=[ModelSettingRecord.area],
            set_={"model": statement.excluded.model, "updated_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)
