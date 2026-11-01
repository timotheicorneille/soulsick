"""Fichiers de paroles d'une piste : même nom qu'elle, extension de paroles (Navidrome lit le .lrc).

Ils suivent leur piste et prennent son nouveau nom. Quand une piste en remplace une autre,
les paroles déjà présentes l'emportent : elles ont pu être recalées à la main.
"""

import os
import shutil

# Dans l'ordre de préférence quand une piste en a plusieurs ; .lrc et .elrc sont horodatés
LYRICS_EXTENSIONS = ("lrc", "elrc", "txt")
SYNCED_EXTENSIONS = {"lrc", "elrc"}
# Téléchargés par la chasse même sans les pochettes ; pas .txt, trop souvent autre chose
DOWNLOADED_LYRICS = SYNCED_EXTENSIONS


def kind_of_file(ext: str | None) -> str | None:
    """« synced » ou « plain » pour un fichier de paroles de cette extension."""
    if not ext:
        return None
    return "synced" if ext in SYNCED_EXTENSIONS else "plain"


def best_sidecar(exts: set[str]) -> str | None:
    """L'extension du fichier de paroles retenu parmi ceux d'une piste."""
    return next((e for e in LYRICS_EXTENSIONS if e in exts), None)


def lyrics_of(track: str) -> list[str]:
    """Les fichiers de paroles de `track`, à côté d'elle."""
    folder, name = os.path.split(track)
    stem = os.path.splitext(name)[0]
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return []
    out = []
    for n in sorted(names):
        s, ext = os.path.splitext(n)
        if s == stem and ext[1:].lower() in LYRICS_EXTENSIONS and os.path.isfile(os.path.join(folder, n)):
            out.append(os.path.join(folder, n))
    return out


def _renamed(lyrics: str, track: str) -> str:
    return os.path.splitext(track)[0] + os.path.splitext(lyrics)[1]


def follow(src: str, dest: str) -> list[str]:
    """Les paroles de la piste `src` vont à côté de `dest`, sous son nom. Celles déjà
    là-bas restent : les nouvelles sont alors laissées où elles sont."""
    moved = []
    for lyr in lyrics_of(src):
        target = _renamed(lyr, dest)
        if os.path.realpath(lyr) == os.path.realpath(target) or os.path.exists(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(lyr, target)
        moved.append(target)
    return moved


def drop_replaced(old: str, new: str) -> None:
    """La piste `old` est remplacée par `new` (autre nom) : ses paroles passent à `new`,
    à la place de celles arrivées avec elle ; puis `old` est supprimée."""
    for lyr in lyrics_of(old):
        target = _renamed(lyr, new)
        if os.path.realpath(lyr) != os.path.realpath(target):
            if os.path.exists(target):
                os.remove(target)
            shutil.move(lyr, target)
    os.remove(old)

