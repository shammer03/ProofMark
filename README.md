[README.md](https://github.com/user-attachments/files/32878636/README.md)
# ProofMark

Darkroom contact-sheet proofing with wax grease-pencil marks (Fedora, PySide6).

## Run
    sudo dnf install python3-pyside6 python3-pillow python3-rawpy perl-Image-ExifTool
    python3 proofmark.py            # python3 proofmark.py --version

## Release workflow
    ./release.py bump minor "What changed"    # version, build number, changelog, spec + AppStream
    git commit -am "Release vX.Y.Z" && git tag vX.Y.Z && git push --tags
    ./release.py build                        # dist/: attach proofmark.py + .sha256 to the GitHub release
    ./release.py icon                         # writes dist/proofmark-512.png (copy to packaging/)

Set `UPDATE_REPO = "owner/repo"` in proofmark.py (or Settings ▸ Updates) to enable in-app update checks.

## Getting into Fedora / Flathub
1. **Now:** publish on GitHub, then create a **COPR** repo from `packaging/proofmark.spec`.
   Users run `sudo dnf copr enable you/proofmark && sudo dnf install proofmark`.
2. **Flathub:** finish `packaging/io.github.shammer03.ProofMark.yml` (pin wheel/exiftool hashes),
   add screenshots + homepage URL to the metainfo, then open a submission at github.com/flathub/flathub.
   Flatpak apps appear in GNOME Software, which is what most people mean by the "Fedora app store".
3. **Official Fedora repos:** submit the RPM for package review once it is stable.

Licence: GPL-3.0-or-later, © 2026 S. Hambrick (see `LICENSE`). Still to do before Flathub: add a screenshot
to the metainfo and pin the Pillow / exiftool hashes in the Flatpak manifest.
