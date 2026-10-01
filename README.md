# ProofMark

Darkroom contact-sheet proofing with wax grease-pencil marks (Fedora, PySide6).

## Run
    sudo dnf install python3-pyside6 python3-pillow perl-Image-ExifTool
    pip install --user rawpy        # optional, for RAW files (Fedora has no python3-rawpy package)
    python3 proofmark.py            # python3 proofmark.py --version

## Your files are never modified
ProofMark is non-destructive, like RapidRAW. For `photo.jpg` it writes only sidecars next to it:

- `photo.jpg.pmdata` — ProofMark's grease marks, rating, film and gear (JSON)
- `photo.jpg.rrdata` — RapidRAW's sidecar. ProofMark only adds Artist, Copyright and a
  "Film · Camera · Lens" UserComment to its `exif` section; RapidRAW applies them when it exports.
  Your RapidRAW edits, rating and tags are kept, and a field you change in RapidRAW is never overwritten.
- `photo.xmp` — rating and marks for other apps (an existing `.xmp` from another app is left alone)
- `.proofmark-roll.json` in the roll folder — roll title, shot date, lab, keywords, notes and frame order

exiftool is optional: it is only used to read RAW files' EXIF the first time a `.rrdata` is created.

## Release workflow
    ./release.py bump minor "What changed"    # version, build number, changelog, spec + AppStream
    git commit -am "Release vX.Y.Z" && git tag vX.Y.Z && git push --tags
    ./release.py build                        # dist/: attach proofmark.py + .sha256 to the GitHub release
    ./release.py icon                         # redraws packaging/proofmark-512.png

Set `UPDATE_REPO = "owner/repo"` in proofmark.py (or Settings ▸ Updates) to enable in-app update checks.

Licence: GPL-3.0-or-later, © 2026 S. Hambrick (see `LICENSE`).
