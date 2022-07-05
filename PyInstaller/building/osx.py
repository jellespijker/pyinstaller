#-----------------------------------------------------------------------------
# Copyright (c) 2005-2022, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License (version 2
# or later) with exception for distributing the bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#
# SPDX-License-Identifier: (GPL-2.0-or-later WITH Bootloader-exception)
#-----------------------------------------------------------------------------

import os
import plistlib
import shutil

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE
from PyInstaller.building.datastruct import TOC, Target, logger
from PyInstaller.building.utils import (_check_path_overlap, _rmtree, add_suffix_to_extension, checkCache)
from PyInstaller.compat import is_darwin
from PyInstaller.building.icon import normalize_icon_type

if is_darwin:
    import PyInstaller.utils.osx as osxutils


class BUNDLE(Target):
    def __init__(self, *args, **kws):
        from PyInstaller.config import CONF

        # BUNDLE only has a sense under Mac OS, it's a noop on other platforms
        if not is_darwin:
            return

        # Get a path to a .icns icon for the app bundle.
        self.icon = kws.get('icon')
        if not self.icon:
            # --icon not specified; use the default in the pyinstaller folder
            self.icon = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), 'bootloader', 'images', 'icon-windowed.icns'
            )
        else:
            # User gave an --icon=path. If it is relative, make it relative to the spec file location.
            if not os.path.isabs(self.icon):
                self.icon = os.path.join(CONF['specpath'], self.icon)

        Target.__init__(self)

        # .app bundle is created in DISTPATH.
        self.name = kws.get('name', None)
        base_name = os.path.basename(self.name)
        self.name = os.path.join(CONF['distpath'], base_name)

        self.appname = os.path.splitext(base_name)[0]
        self.version = kws.get("version", "0.0.0")
        self.toc = TOC()
        self.strip = False
        self.upx = False
        self.console = True
        self.target_arch = None
        self.codesign_identity = None
        self.entitlements_file = None

        # .app bundle identifier for Code Signing
        self.bundle_identifier = kws.get('bundle_identifier')
        if not self.bundle_identifier:
            # Fallback to appname.
            self.bundle_identifier = self.appname

        self.info_plist = kws.get('info_plist', None)

        for arg in args:
            if isinstance(arg, EXE):
                self.toc.append((os.path.basename(arg.name), arg.name, arg.typ))
                self.toc.extend(arg.dependencies)
                self.strip = arg.strip
                self.upx = arg.upx
                self.upx_exclude = arg.upx_exclude
                self.console = arg.console
                self.target_arch = arg.target_arch
                self.codesign_identity = arg.codesign_identity
                self.entitlements_file = arg.entitlements_file
            elif isinstance(arg, TOC):
                self.toc.extend(arg)
                # TOC doesn't have a strip or upx attribute, so there is no way for us to tell which cache we should
                # draw from.
            elif isinstance(arg, COLLECT):
                self.toc.extend(arg.toc)
                self.strip = arg.strip_binaries
                self.upx = arg.upx_binaries
                self.upx_exclude = arg.upx_exclude
                self.console = arg.console
                self.target_arch = arg.target_arch
                self.codesign_identity = arg.codesign_identity
                self.entitlements_file = arg.entitlements_file
            else:
                logger.info("unsupported entry %s", arg.__class__.__name__)
        # Now, find values for app filepath (name), app name (appname), and name of the actual executable (exename) from
        # the first EXECUTABLE item in toc, which might have come from a COLLECT too (not from an EXE).
        for inm, name, typ in self.toc:
            if typ == "EXECUTABLE":
                self.exename = name
                break
        self.__postinit__()

    _GUTS = (
        # BUNDLE always builds, just want the toc to be written out
        ('toc', None),
    )

    def _check_guts(self, data, last_build):
        # BUNDLE always needs to be executed, since it will clean the output directory anyway to make sure there is no
        # existing cruft accumulating.
        return 1

    def assemble(self):
        from PyInstaller.config import CONF

        if _check_path_overlap(self.name) and os.path.isdir(self.name):
            _rmtree(self.name)
        logger.info("Building BUNDLE %s", self.tocbasename)

        # Create a minimal Mac bundle structure.
        macos_path = Path(self.name, "Contents", "MacOS")
        resources_path = Path(self.name, "Contents", "Resources")
        frameworks_path = Path(self.name, "Contents", "Frameworks")
        os.makedirs(macos_path)
        os.makedirs(resources_path)
        os.makedirs(frameworks_path)

        # Makes sure the icon exists and attempts to convert to the proper format if applicable
        self.icon = normalize_icon_type(self.icon, ("icns",), "icns", CONF["workpath"])

        # Ensure icon path is absolute
        self.icon = os.path.abspath(self.icon)

        # Copy icns icon to Resources directory.
        shutil.copy(self.icon, os.path.join(self.name, 'Contents', 'Resources'))

        # Key/values for a minimal Info.plist file
        info_plist_dict = {
            "CFBundleDisplayName": self.appname,
            "CFBundleName": self.appname,

            # Required by 'codesign' utility.
            # The value for CFBundleIdentifier is used as the default unique name of your program for Code Signing
            # purposes. It even identifies the APP for access to restricted OS X areas like Keychain.
            #
            # The identifier used for signing must be globally unique. The usual form for this identifier is a
            # hierarchical name in reverse DNS notation, starting with the toplevel domain, followed by the company
            # name, followed by the department within the company, and ending with the product name. Usually in the
            # form: com.mycompany.department.appname
            # CLI option --osx-bundle-identifier sets this value.
            "CFBundleIdentifier": self.bundle_identifier,
            "CFBundleExecutable": os.path.basename(self.exename),
            "CFBundleIconFile": os.path.basename(self.icon),
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": self.version,
        }

        # Set some default values. But they still can be overwritten by the user.
        if self.console:
            # Setting EXE console=True implies LSBackgroundOnly=True.
            info_plist_dict['LSBackgroundOnly'] = True
        else:
            # Let's use high resolution by default.
            info_plist_dict['NSHighResolutionCapable'] = True

        # Merge info_plist settings from spec file
        if isinstance(self.info_plist, dict) and self.info_plist:
            info_plist_dict.update(self.info_plist)

        plist_filename = os.path.join(self.name, "Contents", "Info.plist")
        with open(plist_filename, "wb") as plist_fh:
            plistlib.dump(info_plist_dict, plist_fh)

        links = []
        _QT_BASE_PATH = {'PySide2', 'PySide6', 'PyQt5', 'PyQt6', 'PySide6'}
        for inm, fnm, typ in self.toc:
            # Adjust name for extensions, if applicable
            inm, fnm, typ = add_suffix_to_extension(inm, fnm, typ)
            inm_ = Path(inm)
            fnm = Path(fnm)
            # Copy files from cache. This ensures that are used files with relative paths to dynamic library
            # dependencies (@executable_path)
            if typ in ('EXTENSION', 'BINARY') or (typ == 'DATA' and inm_.suffix == '.so'):
                if any(['.' in p for p in inm_.parent.parts]):
                    inm_ = Path(inm_.name)
                fnm = Path(checkCache(
                    str(fnm),
                    strip=self.strip,
                    upx=self.upx,
                    upx_exclude=self.upx_exclude,
                    dist_nm=str(inm_),
                    target_arch=self.target_arch,
                    codesign_identity=self.codesign_identity,
                    entitlements_file=self.entitlements_file,
                    strict_arch_validation=(typ == 'EXTENSION'),
                ))
                frame_dst = frameworks_path.joinpath(inm_)
                if not frame_dst.exists():
                    if frame_dst.is_dir():
                        os.makedirs(frame_dst, exist_ok=True)
                    else:
                        os.makedirs(frame_dst.parent, exist_ok=True)
                shutil.copy(fnm, frame_dst, follow_symlinks=True)
                macos_dst = macos_path.joinpath(inm_)
                if not macos_dst.exists():
                    if macos_dst.is_dir():
                        os.makedirs(macos_dst, exist_ok=True)
                    else:
                        os.makedirs(macos_dst.parent, exist_ok=True)
                    macos_dst.symlink_to(frame_dst)
            else:
                if typ == 'DATA':
                    if any(['.' in p for p in inm_.parent.parts]) or inm_.suffix == '.so':
                        # Skip info dist egg and some not needed folders in tcl and tk, since they all contain dots in their files
                        continue
                    res_dst = resources_path.joinpath(inm_)
                    if not res_dst.exists():
                        if res_dst.is_dir():
                            os.makedirs(res_dst, exist_ok=True)
                        else:
                            os.makedirs(res_dst.parent, exist_ok=True)
                    shutil.copy(fnm, res_dst, follow_symlinks=True)
                    macos_dst = macos_path.joinpath(inm_)
                    if not macos_dst.exists():
                        if macos_dst.is_dir():
                            os.makedirs(macos_dst, exist_ok=True)
                        else:
                            os.makedirs(macos_dst.parent, exist_ok=True)
                        macos_dst.symlink_to(res_dst)
                else:
                    macos_dst = macos_path.joinpath(inm_)
                    if not macos_dst.exists():
                        if macos_dst.is_dir():
                            os.makedirs(macos_dst, exist_ok=True)
                        else:
                            os.makedirs(macos_dst.parent, exist_ok=True)
                        shutil.copy(fnm, macos_dst, follow_symlinks=True)


        # Sign the bundle
        logger.info('Signing the BUNDLE...')
        try:
            osxutils.sign_binary(self.name, self.codesign_identity, self.entitlements_file, deep=True)
        except Exception as e:
            logger.warning("Error while signing the bundle: %s", e)
            logger.warning("You will need to sign the bundle manually!")

        logger.info("Building BUNDLE %s completed successfully.", self.tocbasename)