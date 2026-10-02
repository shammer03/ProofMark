# Fedora RPM spec. Build:  rpmbuild -ba packaging/proofmark.spec   (or submit to COPR)
Name:           proofmark
Version:        1.5.0
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
Recommends:     perl-Image-ExifTool

%description
ProofMark lays out a roll of film scans as a contact sheet with film rebate and
frame numbers, and lets you cull it like a darkroom proof: a loupe, red wax
grease-pencil marks, star ratings and colour labels. Contact sheets print or
export as PDF or images. Original files are never modified; marks and ratings
go to sidecar files that RapidRAW and other XMP-aware apps read.

RAW files open when the optional python3 module rawpy is installed (pip).

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
