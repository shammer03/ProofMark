# Fedora RPM spec. Build:  rpmbuild -ba packaging/proofmark.spec   (or submit to COPR)
Name:           proofmark
Version:        1.2.0
Release:        1%{?dist}
Summary:        Darkroom contact-sheet proofing with wax grease-pencil marks
License:        GPL-3.0-or-later
URL:            https://github.com/shammer03/ProofMark
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
Requires:       python3
Requires:       python3-pyside6
Requires:       python3-pillow
Recommends:     python3-rawpy
Recommends:     perl-Image-ExifTool

%description
ProofMark lays out a roll of film scans as a contact sheet with simulated emulsion
rebate and lets you mark it up with a wax grease pencil. It includes a loupe,
compare mode, equipment inventory, and clean contact sheet printing.

%prep
%autosetup -n ProofMark-%{version}

%install
install -Dpm 0644 proofmark.py %{buildroot}%{_datadir}/%{name}/proofmark.py
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/proofmark <<'WRAP'
#!/bin/sh
exec /usr/bin/python3 %{_datadir}/%{name}/proofmark.py "$@"
WRAP
chmod 0755 %{buildroot}%{_bindir}/proofmark
install -Dpm 0644 packaging/io.github.shammer03.ProofMark.desktop \
    %{buildroot}%{_datadir}/applications/io.github.shammer03.ProofMark.desktop
install -Dpm 0644 packaging/io.github.shammer03.ProofMark.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.shammer03.ProofMark.metainfo.xml
# 512px icon: generate with `./release.py icon` and commit as packaging/proofmark-512.png
install -Dpm 0644 packaging/proofmark-512.png \
    %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/io.github.shammer03.ProofMark.png

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.shammer03.ProofMark.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.shammer03.ProofMark.metainfo.xml

%files
%{_bindir}/proofmark
%{_datadir}/%{name}/
%{_datadir}/applications/io.github.shammer03.ProofMark.desktop
%{_metainfodir}/io.github.shammer03.ProofMark.metainfo.xml
%{_datadir}/icons/hicolor/512x512/apps/io.github.shammer03.ProofMark.png

%changelog
* Wed Sep 30 2026 S. Hambrick <hambrick03@gmail.com> - 1.1.0-1
- Initial package
