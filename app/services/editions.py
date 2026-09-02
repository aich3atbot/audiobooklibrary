"""Edition bookkeeping: the rows that let one book hold several audiobook
recordings (see plan.md "Multi-edition support"). The label is the grouping
name used in library folder names; "" is the unlabelled edition. Relabelling
an edition moves its library folder to the labelled location."""

import logging
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.hardcover import HardcoverClient
from app.config import get_settings
from app.models import Book, Edition, User

logger = logging.getLogger(__name__)

HC_EDITIONS_SHOWN = 10  # books can carry dozens of junk/foreign editions


def get_or_create_edition(
    session: Session,
    book: Book,
    label: str = "",
    hardcover_edition_id: int | None = None,
    narrator: str = "",
) -> Edition:
    """The book's edition with this label, created if absent. Backfills
    Hardcover identity onto an existing row when it was label-only. Caller
    commits."""
    for edition in book.editions:
        if edition.label == label:
            if hardcover_edition_id is not None and edition.hardcover_edition_id is None:
                edition.hardcover_edition_id = hardcover_edition_id
            if narrator and not edition.narrator:
                edition.narrator = narrator
            return edition
    edition = Edition(
        book=book,
        label=label,
        hardcover_edition_id=hardcover_edition_id,
        narrator=narrator,
    )
    session.add(edition)
    return edition


def relabel_edition(session: Session, edition: Edition, new_label: str) -> Path | None:
    """Change an edition's label and move its library folder to the label's
    location (filesystem first, DB committed only after the move succeeds).
    AudioFile rel_paths are relative to the folder, so they are untouched.
    Returns the new library dir, or None when the edition has no files.
    Raises ImportFailure without committing on any conflict."""
    # Deferred: importer imports downloads, which imports this module.
    from app.services.importer import ImportFailure, cleanup_empty_parents, edition_dir_for

    new_label = new_label.strip()
    for sibling in edition.book.editions:
        if sibling is not edition and sibling.label == new_label:
            shown = new_label or "unlabelled"
            raise ImportFailure(f'the book already has a "{shown}" edition')
    if new_label == edition.label:
        return Path(edition.library_path) if edition.library_path else None

    if not edition.library_path:
        edition.label = new_label
        session.commit()
        return None

    library_dir = get_settings().library_dir
    old_dir = Path(edition.library_path)
    edition.label = new_label  # pending in the session until the move succeeds
    new_dir = edition_dir_for(edition)
    try:
        if new_dir == old_dir:
            session.commit()
            return new_dir
        if new_dir.exists():
            if any(new_dir.iterdir()):
                raise ImportFailure(f"destination already exists: {new_dir}")
            new_dir.rmdir()  # empty leftover; rename must create it
        if old_dir.is_dir():
            if not old_dir.resolve().is_relative_to(library_dir.resolve()):
                raise ImportFailure(f"refusing to move a folder outside the library: {old_dir}")
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                old_dir.rename(new_dir)
            except OSError:
                shutil.move(str(old_dir), str(new_dir))
            cleanup_empty_parents(old_dir.parent, library_dir)
        # A missing old folder is not an error: the path is simply repointed
        # (the detail page only offers renames for imported editions).
    except Exception:
        session.rollback()  # un-stage the label change
        raise
    edition.library_path = str(new_dir)
    session.commit()
    logger.info("Relabelled edition of %s -> %r (%s)", edition.book.title, new_label, new_dir)
    return new_dir


def series_edition_candidates(session: Session, book: Book) -> list[dict[str, str]]:
    """Edition labels sibling books in the series already have but this book
    lacks, each with a representative narrator — the "Add existing edition"
    section of the pickers. Standalone books have no siblings."""
    if book.series_id is None:
        return []
    own = {e.label for e in book.editions if e.label}
    rows = session.execute(
        select(Edition.label, Edition.narrator)
        .join(Book)
        .where(Book.series_id == book.series_id, Book.id != book.id, Edition.label != "")
        .order_by(Edition.label)
    ).all()
    candidates: dict[str, str] = {}
    for label, narrator in rows:
        if label in own:
            continue
        if label not in candidates or (not candidates[label] and narrator):
            candidates[label] = narrator or ""
    return [{"label": label, "narrator": narrator} for label, narrator in candidates.items()]


def fetch_hc_editions(
    user: User, hardcover_id: int, book: Book | None = None
) -> tuple[list[dict], str | None]:
    """The Hardcover book's audiobook editions, or a warning when the fetch
    fails/finds nothing — the picker then degrades to a free-form label.
    `book` is the local row when one exists (collection imports may be picking
    an edition for a book nobody tracks yet)."""
    if not user.hardcover_token:
        return [], "No Hardcover token set — enter a label instead."
    try:
        with HardcoverClient(user.hardcover_token) as client:
            editions = client.fetch_editions(hardcover_id)
    except Exception:
        logger.exception("Hardcover editions fetch failed for book %s", hardcover_id)
        return [], "Could not fetch Hardcover's editions — enter a label instead."
    if not editions:
        return [], "Hardcover lists no audiobook editions — enter a label instead."
    if book is None:
        return editions, None
    # Hide editions the book already has (matched by Hardcover id or label) —
    # they'd only offer a duplicate download. An empty default_label never
    # matches, or every no-narrator option would vanish behind the unlabelled
    # edition.
    have_ids = {e.hardcover_edition_id for e in book.editions if e.hardcover_edition_id}
    have_labels = {e.label for e in book.editions if e.label}
    editions = [
        e
        for e in editions
        if e["id"] not in have_ids and (e["default_label"] or None) not in have_labels
    ]
    if not editions:
        return [], "All of Hardcover's audiobook editions are already downloaded — enter a label instead."
    return editions, None


def edition_sections(
    session: Session, user: User, hardcover_id: int, book: Book | None = None
) -> tuple[list[dict], list[dict], str | None]:
    """The picker's sections: sibling-series labels this book lacks (matched
    to the book's Hardcover editions by default label when possible) and the
    remaining Hardcover editions. Sibling labels survive a failed Hardcover
    fetch — they then offer the label with the sibling's narrator. A book that
    exists only on Hardcover has no siblings to offer."""
    editions, warning = fetch_hc_editions(user, hardcover_id, book)
    by_label: dict[str, dict] = {}
    for e in editions:
        # first occurrence wins = highest users_count (Hardcover's fetch order)
        if e["default_label"] and e["default_label"] not in by_label:
            by_label[e["default_label"]] = e
    existing_options: list[dict] = []
    used: set[int] = set()
    for cand in series_edition_candidates(session, book) if book is not None else []:
        hc = by_label.get(cand["label"])
        if hc is not None:
            used.add(hc["id"])
            existing_options.append({"label": cand["label"], "narrator": hc["narrator"], "hc": hc})
        else:
            existing_options.append({"label": cand["label"], "narrator": cand["narrator"], "hc": None})
    rest = [e for e in editions if e["id"] not in used][:HC_EDITIONS_SHOWN]
    return existing_options, rest, warning


def ns_field(base: str, ns: str = "") -> str:
    """A picker field's form name. The Imports page posts every row's fields in
    one request, so each row namespaces its picker with the entry's rel path;
    the single-book pickers pass no namespace and keep the plain names."""
    return f"{base}__{ns}" if ns else base


CUSTOM_OPTION = "custom"  # the Imports picker's "my own label" radio


def edition_choice(form, ns: str = "", pick_wins: bool = False) -> tuple[str, int | None, str]:
    """(label, hardcover_edition_id, narrator) from a submitted edition picker.

    `pick_wins` is how the label box and the picked option interact, which
    differs by picker:
    - False (the download pickers): the box is a bare *override* sitting below
      the options, so a typed label beats the pick and an empty one falls back
      to the picked edition's default label.
    - True (the Imports row): the box belongs to the picker's own CUSTOM_OPTION
      radio, so whichever radio is selected decides. Text left in the box from
      a change of mind is ignored unless that radio is the one selected."""
    label = str(form.get(ns_field("edition_label", ns)) or "").strip()
    raw = str(form.get(ns_field("hc_edition", ns)) or "")
    # options that aren't a Hardcover edition carry a prefixed value: "sib_N"
    # for an unmatched sibling-series label, "rep_N" for one of the book's own
    # editions. Both name a label but no Hardcover edition.
    hc_id = int(raw) if raw.isdigit() else None
    narrator = str(form.get(ns_field(f"hcnarr_{raw}", ns)) or "").strip() if raw else ""
    if not narrator:
        narrator = str(form.get(ns_field("narrator", ns)) or "").strip()
    picked_label = str(form.get(ns_field(f"hclabel_{raw}", ns)) or "").strip() if raw else ""
    if pick_wins:
        if raw and raw != CUSTOM_OPTION:
            label = picked_label
    elif not label and raw:
        label = picked_label
    return label, hc_id, narrator


def replace_choice(form, ns: str = "") -> int | None:
    """The id of the edition an Imports row is replacing, when its picker's
    selection is one of the book's own editions ("rep_<edition id>")."""
    raw = str(form.get(ns_field("hc_edition", ns)) or "")
    rest = raw[len("rep_"):] if raw.startswith("rep_") else ""
    return int(rest) if rest.isdigit() else None


def suggest_labels(session: Session, book: Book) -> list[str]:
    """Labels already in use across the book's series, so edition groups line
    up ("Narrator" for book 3 once books 1-2 use it). Standalone books
    suggest nothing beyond their own labels."""
    query = select(Edition.label).join(Book).where(Edition.label != "").distinct()
    if book.series_id is not None:
        query = query.where(Book.series_id == book.series_id)
    else:
        query = query.where(Book.id == book.id)
    return sorted(session.scalars(query))
