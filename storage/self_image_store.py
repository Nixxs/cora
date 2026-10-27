"""How Cora pictures herself: the images she chose to represent her and her own words for them.

    python -m storage.self_image_store                                   # show what's saved
    python -m storage.self_image_store set self --image FILE --description "..."
    python -m storage.self_image_store export work FILE                  # write an image out
"""

import argparse
import json
import mimetypes
from pathlib import Path
import sys

from dotenv import load_dotenv
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .database import build_engine
from .models import SelfImageRecord

KINDS = ("self", "work")
DESCRIPTION_MAX_CHARS = 1000


class SelfImageStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def set(self, kind: str, description: str, image: bytes | None = None, image_type: str | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        description = " ".join(description.split())
        if not description or len(description) > DESCRIPTION_MAX_CHARS:
            raise ValueError(f"A description needs 1 to {DESCRIPTION_MAX_CHARS} characters")
        if (image is None) != (image_type is None):
            raise ValueError("An image needs its type")
        values = {"description": description, "image": image, "image_type": image_type}
        statement = insert(SelfImageRecord).values(kind=kind, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[SelfImageRecord.kind], set_={**values, "chosen_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)

    def get(self, kind: str) -> SelfImageRecord | None:
        with Session(self.engine, expire_on_commit=False) as session:
            return session.get(SelfImageRecord, kind)

    def descriptions(self) -> dict[str, str]:
        with Session(self.engine) as session:
            return dict(session.execute(select(SelfImageRecord.kind, SelfImageRecord.description)).all())

    def get_startup_context(self) -> str:
        """Her own words for how she pictures herself, for her prompts; empty if she hasn't chosen."""
        descriptions = self.descriptions()
        if not descriptions:
            return ""
        return json.dumps(
            {kind: descriptions[kind] for kind in KINDS if kind in descriptions}, ensure_ascii=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Show or set how Cora pictures herself.")
    sub = parser.add_subparsers(dest="command")
    setter = sub.add_parser("set", help="save an image and her description of it")
    setter.add_argument("kind", choices=KINDS)
    setter.add_argument("--description", required=True)
    setter.add_argument("--image", type=Path)
    exporter = sub.add_parser("export", help="write a saved image to a file")
    exporter.add_argument("kind", choices=KINDS)
    exporter.add_argument("file", type=Path)
    args = parser.parse_args()
    load_dotenv()
    engine = build_engine()
    try:
        store = SelfImageStore(engine)
        if args.command == "set":
            image = args.image.read_bytes() if args.image else None
            image_type = (mimetypes.guess_type(args.image.name)[0] or "image/png") if args.image else None
            store.set(args.kind, args.description, image, image_type)
            print(f"Saved her {args.kind} image.")
        elif args.command == "export":
            record = store.get(args.kind)
            if record is None or record.image is None:
                sys.exit(f"No {args.kind} image saved.")
            args.file.write_bytes(record.image)
            print(f"Wrote {args.file}")
        else:
            for kind in KINDS:
                record = store.get(kind)
                if record is None:
                    print(f"{kind}: not chosen")
                else:
                    size = f"{len(record.image) / 1000:.0f} kB {record.image_type}" if record.image else "no image"
                    print(f"{kind} ({size}, chosen {record.chosen_at:%Y-%m-%d}): {record.description}")
    except ValueError as error:
        sys.exit(str(error))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
