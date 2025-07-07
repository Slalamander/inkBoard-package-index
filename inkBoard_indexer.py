"""Script to automatically create packages of all integrations and platforms present in the designer and track their versions

Meant to be used with github workflows
"""
from typing import Generator
import os
import re
import json
import logging
from pathlib import Path
import shutil
import tempfile
import zipfile
import argparse
from contextlib import suppress

from datetime import datetime as dt

import inkBoard
from inkBoard.logging import ColorFormatter
from inkBoard import constants
from inkBoard.constants import DEBUGGING
from inkBoard.types import manifestjson, platformjson
from inkBoard.packaging.constants import ZIP_COMPRESSION, ZIP_COMPRESSION_LEVEL
from inkBoard.packaging.version import parse_version

import inkBoarddesigner
import PythonScreenStackManager

print("Successfully imported everything")

_LOGGER = inkBoard.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

LOGGER_FORMAT = '${asctime} [${levelname}]: ${message}'
LOGGER_DATE_FORMAT = '%H:%M:%S'

INDEX_FOLDER = Path(__file__).parent
if DEBUGGING:
    print("Index running in debug mode")
    _LOGGER.info("Running in DEBUG")
    INDEX_FOLDER = INDEX_FOLDER / "debug_index"
    if not INDEX_FOLDER.exists(): INDEX_FOLDER.mkdir()

INDEX_FILE = INDEX_FOLDER / "index.json"

INTEGRATION_INDEX_FOLDER = INDEX_FOLDER / "integrations"
PLATFORM_INDEX_FOLDER = INDEX_FOLDER / "platforms"
ARCHIVE_FOLDER_STR = "versions"

DEV_PATTERN = r"([0-9.]+)_dev.zip"
MAIN_PATTERN = r"([0-9.]+).zip"

if not INTEGRATION_INDEX_FOLDER.exists(): INTEGRATION_INDEX_FOLDER.mkdir()
if not PLATFORM_INDEX_FOLDER.exists(): PLATFORM_INDEX_FOLDER.mkdir()

if INDEX_FILE.exists():
    with open(INDEX_FILE, "r") as file:
        current_index = json.load(file)
else:
    current_index = {
        "inkBoard": inkBoard.__version__,
        "PythonScreenStackManager": PythonScreenStackManager.__version__,
        "inkBoarddesigner": inkBoarddesigner.__version__,
        "timestamp": dt.fromtimestamp(0).isoformat(),
        "platforms": {},
        "integrations": {"api": {"main": "1.0.0"}},
        }

integration_index = current_index["integrations"].copy()
platform_index = current_index["platforms"].copy()

class inkBoardIndexingError(Exception):
    "Base exception for errors in the indexing process"

class ArchivingError(inkBoardIndexingError):
    "Something went wrong in the archiving of an old package"

class PackagingError(inkBoardIndexingError):
    "Something went wrong packaging a platform or integration"

class VersionError(inkBoardIndexingError):
    "Mismatch in a version somewhere"

class FileIndexError(inkBoardIndexingError):
    "A file is missing, or present when it should not be"

def parse_arguments():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--dev', action='store_true', dest='dev',
                        help="Assumed zip packages created are dev packages, and appends _dev to the packages",
                        default=DEBUGGING)
    return parser.parse_args()

def gather_folders(base_folder) -> Generator[Path, None, None]:
    """Gathers all folders in the base_folder, provided they do not start with an `_`"""

    for p in Path(base_folder).iterdir():
        if p.is_dir() and not p.name.startswith("_"):
            yield p

def _create_integration_index(dev_mode: bool):
    folder = constants.DESIGNER_FOLDER / "integrations"
    int_folders = gather_folders(folder)
    for p in int_folders:
        manifest_file = p / "manifest.json"
        if not manifest_file.exists():
            _LOGGER.warning(f"No manifest file for integration folder {p}")
            continue

        key = "dev" if dev_mode else "main"
        with open(manifest_file) as file:
            d = manifestjson(**json.load(file))

        if p.name in integration_index:
            old_version = parse_version(integration_index[p.name].get(key, "0.0.0"))
            integration_index[p.name][key] = d["version"]
        else:
            old_version = parse_version("0.0.0")
            integration_index[p.name] = {key: d["version"]}

        pack_pattern = p.name + r'(?P<version>(([0-9]+\.){2}[0-9]+))' + r"\.zip"

        ##Maybe change this pattern? HA for betas uses 2025.6.0b1 etc
        pack_dev_pattern = p.name + r'(?P<version>(([0-9]+\.){2}[0-9]+)|(([0-9]+\.){3}[ab][0-9]+))' + r"_dev\.zip"   ##dev version matches both for normal versioning AND beta versioning
        
        integration_folder = INTEGRATION_INDEX_FOLDER / p.name
        if not integration_folder.exists():
            _LOGGER.info(f"Making folder for integration {p.name}")
            integration_folder.mkdir()
            (integration_folder / ARCHIVE_FOLDER_STR).mkdir()
        
        make_package = True
        if dev_mode:
            package_name =  integration_folder / f"{p.name}-{d['version']}_dev.zip"
            pattern = p.name + DEV_PATTERN
            pack_pattern = re.compile(pack_dev_pattern)
        else:
            package_name = integration_folder / f"{p.name}-{d['version']}.zip"
            pattern = p.name + DEV_PATTERN
            # pack_glob = f"{p.name}-*.zip"
            # pack_pattern = p.name + r'((([0-9]+\.){2}[0-9]+))' + r"\.zip"
            pack_pattern = re.compile(pack_pattern)
            if package_name.exists():
                make_package = False
        
        # pattern = re.compile(pattern)

        # if not (package_name).exists():

        if make_package:
            #[x]: older versions can be moved to a "versions" folder
            ##      overwrite: yes, unless dev_mode True and not a pattern match
            archive_old = False

            current_file = [f for f in integration_folder.glob("*.zip") if pack_pattern.match(f.name)]
            if len(current_file) > 1:
                raise OSError("Found more than one package")
            elif len(current_file) == 1:
                ##Take care of copying and renaming the correct file
                ##If in dev mode: check if the current dev file version is a beta version
                ##If so: can copy; If not: only copy if the version package does not yet exist?
                ##Means: if i.e. 0.1.1 already has a package in the version list: 
                ##          It is either from main; or from the dev branch. So may enable downloading of untested versions.
                ##          So no, will not do so. Aside from the fact that archiving dev versions may be disabled too at some point.
                ##          Do log a warning I guess? Although I could probably not see that lol
                
                ##parse_version should generally use packaging I think?
                ##Which means: https://packaging.python.org/en/latest/specifications/version-specifiers/#pre-releases holds for versioning
                ##pre_release is true for things like X.YaN; dev release is true for X.Y.devN

                if dev_mode:
                    current_file = current_file[0]
                    file_version_str = pack_pattern.match(current_file.name).group("version")
                    file_version = parse_version(file_version_str)
                    if file_version.is_devrelease or file_version.is_prerelease:
                        ##We copy
                        archive_file_name = p.name + file_version + ".zip"
                        archive_old = True
                else:
                    ##Not in dev mode. No checks needed on the currently present file.
                    ##Should not be a dev release (check happened in previous run)
                    ##File should never be deleted and always copied
                    current_file = current_file[0]
                    archive_old = True
                    archive_file_name = current_file.name
                
            if archive_old:
                archive_dst = integration_folder / ARCHIVE_FOLDER_STR / archive_file_name
                if archive_dst.exists() and not dev_mode:
                    raise FileExistsError("Should not replace older versions that are not dev modes")
                archive_dst = current_file.replace(archive_dst)
                # replace does not care about already existing files, change rename if that is desired
                _LOGGER.info(f"Archived old version of integration {p.name} to {archive_dst}")
            else:
                ##Determine when to remove an old file...
                ##Honestly, for the entire logic, sketch a flow diagram cause this is not working.
                ##Should include:
                ##  - When to make a package
                ##  - When to archive the old package
                ##  - When to remove the old package
            
                ##Conditions to remove the old zip file:
                ##archive_old is False (it has already been moved otherwise)
                ##dev_mode is True (If dev_mode is False, archive_old should automatically be True)
                ##current_file is not a dev version (for example, file 0.2.0_dev will not be moved, since it is not a dev version. Meaning it should be removed if the new dev version is something else.)
                ##current file version is different from the old version

                ##What to do if dev_mode and current version are the same though?
                ##Tbh, only the first iteration of a version should be packaged...
                ##Because otherwise the contents of a version will not be consistent, which you simply don't want.
                pass

            create_integration_zip(p, package_name)

            # for file in (INTEGRATION_INDEX_FOLDER / p.name).glob(f"*.zip"):
            #     if pattern.match(file.name) and file.name != package_name.name:
            #         print(f"Removing outdated integration package {file.name}")
            #         os.remove(file)
    return integration_index

def create_integration_index(dev_mode: bool):

    pack_type = "integration"
    folder = constants.DESIGNER_FOLDER / "integrations"
    int_folders = gather_folders(folder)
    err_dict = {}
    for p in int_folders:
        manifest_file = p / "manifest.json"
        if not manifest_file.exists():
            msg = f"No manifest file for {pack_type} folder {p}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        branch = "dev" if dev_mode else "main"
        with open(manifest_file) as file:
            d = manifestjson(**json.load(file))

        index_folder = INTEGRATION_INDEX_FOLDER / p.name
        old_package = None
        make_package = False
        archive_old_package = False

        manifest_version = parse_version(d["version"])
        if p.name in integration_index:
            index_version = parse_version(integration_index[p.name].get(branch, "0.0.0"))
            integration_index[p.name][branch] = d["version"]
        else:
            index_version = parse_version("0.0.0")
            integration_index[p.name] = {branch: d["version"]}

        if index_version == manifest_version:
            ##Same version, means it does not have to be made. Only make for new versions
            _LOGGER.debug(f"{pack_type.capitalize()} {p.name} did not change version")
            continue
        elif index_version > manifest_version:
            ##Version went down. Should not happen and is weird.
            msg = f"{pack_type.capitalize()} {p.name} has an index version larger than the current manifest version"
            _LOGGER.error(msg)
            err_dict[p.name] = VersionError(msg)
            continue
        elif branch == "main" and manifest_version.is_prerelease:
            ##branch cannot be main and return a prerelease version
            msg = f"{pack_type.capitalize()} {p.name} has a prerelease version in the main branch"
            _LOGGER.error(msg)
            err_dict[p.name] = VersionError(msg)
            continue

        if not index_folder.exists():
            _LOGGER.info(f"Making folder for {pack_type} {p.name}")
            index_folder.mkdir()
            (index_folder / ARCHIVE_FOLDER_STR).mkdir()
            make_package = True

            if dev_mode:
                package_name =  index_folder / f"{p.name}-{manifest_version}_dev.zip"
            else:
                package_name =  index_folder / f"{p.name}-{manifest_version}.zip"

        elif dev_mode:
            make_package = True
            package_name =  index_folder / f"{p.name}-{manifest_version}_dev.zip"
            old_package = index_folder / f"{p.name}-{index_version}_dev.zip"
            if old_package.exists() and index_version.is_prerelease:
                ##Do not want to archive mainline versions which do not come from the main branch
                archive_package = index_folder / ARCHIVE_FOLDER_STR / f"{p.name}-{index_version}.zip"
                archive_old_package = True
        else:
            make_package = True
            package_name = index_folder / f"{p.name}-{manifest_version}.zip"
            old_package = index_folder / f"{p.name}-{index_version}.zip"
            if old_package.exists():
                archive_package = index_folder / ARCHIVE_FOLDER_STR / f"{p.name}-{index_version}.zip"
                archive_old_package = True

        if archive_old_package:
            if archive_package.exists():
                ##Check if the version does not exist in the archive yet
                msg = f"Archived {pack_type} package file {archive_package.name} already exists"
                _LOGGER.error(msg + ". Not Archiving")
                err_dict[p.name] = FileIndexError(msg)
                continue
            
            _LOGGER.info(f"Archiving old {pack_type} package {old_package.name} to {archive_package.name}")
            old_package.replace(archive_package)
        elif old_package and old_package.exists():
            ##If branch == "main", the exists check is already performed and causes archive to be set to True.
            _LOGGER.info(f"Removing old {pack_type} package {old_package.name}")
            os.remove(old_package)

        if len(list(index_folder.glob("*.zip"))) > 1:
            ##Check to see if the current folder structure is ok to make a new package in
            msg = f"There are two or more packages in the main folder {index_folder} of {pack_type} {p.name} now, will not create new {pack_type} package {package_name.name}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        if make_package:
            create_integration_zip(p, package_name)

    if err_dict:
        d = {}
        for k, v in err_dict.items():
            d.setdefault(v, 0)
            d[v] += 1
        msg = f"Errors while creating {pack_type} index: {d}. See logs for more details"
        raise inkBoardIndexingError(msg)
    return integration_index

def create_platform_index(dev_mode: bool):
    pack_type = "platform"
    folder = constants.DESIGNER_FOLDER / "platforms"
    int_folders = gather_folders(folder)
    err_dict = {}
    for p in int_folders:
        platform_file = p / "platforms.json"
        if not platform_file.exists():
            msg = f"No platform file for {pack_type} folder {p}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        branch = "dev" if dev_mode else "main"
        with open(platform_file) as file:
            d = platformjson(**json.load(file))

        index_folder = INTEGRATION_INDEX_FOLDER / p.name
        old_package = None
        make_package = False
        archive_old_package = False

        platform_version = parse_version(d["version"])
        if p.name in platform_index:
            index_version = parse_version(platform_index[p.name].get(branch, "0.0.0"))
            platform_index[p.name][branch] = d["version"]
        else:
            index_version = parse_version("0.0.0")
            platform_index[p.name] = {branch: d["version"]}

        if index_version == platform_version:
            ##Same version, means it does not have to be made. Only make for new versions
            _LOGGER.debug(f"{pack_type.capitalize()} {p.name} did not change version")
            continue
        elif index_version > platform_version:
            ##Version went down. Should not happen and is weird.
            msg = f"{pack_type.capitalize()} {p.name} has an index version larger than the current platform version"
            _LOGGER.error(msg)
            err_dict[p.name] = VersionError(msg)
            continue
        elif branch == "main" and platform_version.is_prerelease:
            ##branch cannot be main and return a prerelease version
            msg = f"{pack_type.capitalize()} {p.name} has a prerelease version in the main branch"
            _LOGGER.error(msg)
            err_dict[p.name] = VersionError(msg)
            continue

        if not index_folder.exists():
            _LOGGER.info(f"Making folder for {pack_type} {p.name}")
            index_folder.mkdir()
            (index_folder / ARCHIVE_FOLDER_STR).mkdir()
            make_package = True

            if dev_mode:
                package_name =  index_folder / f"{p.name}-{platform_version}_dev.zip"
            else:
                package_name =  index_folder / f"{p.name}-{platform_version}.zip"

        elif dev_mode:
            make_package = True
            package_name =  index_folder / f"{p.name}-{platform_version}_dev.zip"
            old_package = index_folder / f"{p.name}-{index_version}_dev.zip"
            if old_package.exists() and index_version.is_prerelease:
                ##Do not want to archive mainline versions which do not come from the main branch
                archive_package = index_folder / ARCHIVE_FOLDER_STR / f"{p.name}-{index_version}.zip"
                archive_old_package = True
        else:
            make_package = True
            package_name = index_folder / f"{p.name}-{platform_version}.zip"
            old_package = index_folder / f"{p.name}-{index_version}.zip"
            if old_package.exists():
                archive_package = index_folder / ARCHIVE_FOLDER_STR / f"{p.name}-{index_version}.zip"
                archive_old_package = True

        if archive_old_package:
            if archive_package.exists():
                ##Check if the version does not exist in the archive yet
                msg = f"Archived {pack_type} package file {archive_package.name} already exists"
                _LOGGER.error(msg + ". Not Archiving")
                err_dict[p.name] = FileIndexError(msg)
                continue
            
            _LOGGER.info(f"Archiving old {pack_type} package {old_package.name} to {archive_package.name}")
            old_package.replace(archive_package)
        elif old_package and old_package.exists():
            ##If branch == "main", the exists check is already performed and causes archive to be set to True.
            _LOGGER.info(f"Removing old {pack_type} package {old_package.name}")
            os.remove(old_package)

        if len(list(index_folder.glob("*.zip"))) > 1:
            ##Check to see if the current folder structure is ok to make a new package in
            msg = f"There are two or more packages in the main folder {index_folder} of {pack_type} {p.name} now, will not create new {pack_type} package {package_name.name}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        if make_package:
            create_platform_zip(p, package_name)

    if err_dict:
        d = {}
        for k, v in err_dict.items():
            d.setdefault(v, 0)
            d[v] += 1
        msg = f"Errors while creating {pack_type} index: {d}. See logs for more details"
        raise inkBoardIndexingError(msg)
    return platform_index


def _create_platform_index(dev_mode: bool):
    folder = constants.DESIGNER_FOLDER / "platforms"
    plt_folders = gather_folders(folder)
    for p in plt_folders:
        platform_file = p / "platforms.json"
        if not platform_file.exists():
            continue

        key = "dev" if dev_mode else "main"

        with open(platform_file) as file:
            d = platformjson(**json.load(file))

        if p.name in platform_index:
            platform_index[p.name][key] = d["version"]
        else:
            platform_index[p.name] = {key: d["version"]}

        if dev_mode:
            package_name = PLATFORM_INDEX_FOLDER / f"{p.name}{d['version']}_dev.zip"
            pattern = p.name + DEV_PATTERN
        else:
            package_name = PLATFORM_INDEX_FOLDER / f"{p.name}{d['version']}.zip"
            pattern = p.name + MAIN_PATTERN
        
        pattern = re.compile(pattern)
        
        if not (package_name).exists():
            create_platform_zip(p, package_name)

            for file in PLATFORM_INDEX_FOLDER.glob("*.zip"):
                if pattern.match(file.name) and file.name != package_name.name:
                    print(f"Removing outdated platform package {file.name}")
                    os.remove(file)

    return platform_index

##for the zips, would they only be for inkBoard, and not the designer?
##I.e. should the zips not hold any data that would be omitted when downloading
##I think, for now, yes. -> also base installer handles the installation anyways so only the downloading really matters

##If so, omit from integrations: designer.py/designer folder
##from platforms: any of the manual files.
##packge_files folder will be included, but should only be unpacked based on prompt input
##readme is omitted as it should be in the docs anyways
##always omit __pycache__

def ignore_files(src, names):
    """Returns a list with files to not copy for `shutil.copytree`

    Parameters
    ----------
    parentbase_folder : Path
        The base folder being copied from
    src : str
        source path, passed by `copytree`
    names : list[str]
        list with file and folder names, passed by `copytree`
    ignore_in_baseparent_folder : set, optional
        Set with filenames to ignore (i.e. not copy), _Only if_ the parent folder of `src` is `base_ignore_folder`, by default {}

    Returns
    -------
    _type_
        _description_
    """
    return {"__pycache__"}

def create_integration_zip(integration_folder: Path, zip_file_path: Path):

    ##As per packaging: first create temp directory
    ##When that is done, put all files in there into the zipfile

    with tempfile.TemporaryDirectory(dir=str(INTEGRATION_INDEX_FOLDER)) as tempdir:
        name = integration_folder.name
        _LOGGER.info(f"Gathering integration {name}")
        shutil.copytree(
            src = integration_folder,
            dst= Path(tempdir) / name,
            ignore=lambda *args: ("__pycache__","emulator.json", "designer", "designer.py")
        )

        _LOGGER.debug(f"Zipping up integration {name} to {zip_file_path}")
        with zipfile.ZipFile(zip_file_path, 'w', ZIP_COMPRESSION, compresslevel=ZIP_COMPRESSION_LEVEL) as zip_file:
            for foldername, subfolders, filenames in os.walk(tempdir):
                _LOGGER.verbose(f"Zipping contents of folder {foldername}")
                for filename in filenames:
                    file_path = os.path.join(foldername, filename)
                    zip_file.write(file_path, os.path.relpath(file_path, tempdir))
                for dir in subfolders:
                    dir_path = os.path.join(foldername, dir)
                    zip_file.write(dir_path, os.path.relpath(dir_path, tempdir))
        _LOGGER.info(f"Succesfully packaged integration {name}")
    return

def create_platform_zip(platform_folder: Path, zip_file_path: Path):
    
    with tempfile.TemporaryDirectory(dir=str(INTEGRATION_INDEX_FOLDER)) as tempdir:
        name = platform_folder.name
        _LOGGER.info(f"Gathering platform {name}")
        shutil.copytree(
            src = platform_folder,
            dst= Path(tempdir) / name,
            ignore=lambda *args: ("__pycache__","designer.py", "designer")
        )

        _LOGGER.debug(f"Zipping up platform {name} to {zip_file_path}")
        with zipfile.ZipFile(zip_file_path, 'w', ZIP_COMPRESSION, compresslevel=ZIP_COMPRESSION_LEVEL) as zip_file:
            for foldername, subfolders, filenames in os.walk(tempdir):
                _LOGGER.verbose(f"Zipping contents of folder {foldername}")
                for filename in filenames:
                    file_path = os.path.join(foldername, filename)
                    zip_file.write(file_path, os.path.relpath(file_path, tempdir))
                for dir in subfolders:
                    dir_path = os.path.join(foldername, dir)
                    zip_file.write(dir_path, os.path.relpath(dir_path, tempdir))
        _LOGGER.info(f"Succesfully packaged platform {name}")
    return

def main():
    streamhandler = logging.StreamHandler()
    streamhandler.setFormatter(ColorFormatter(LOGGER_FORMAT, LOGGER_DATE_FORMAT))
    logging.basicConfig(
        format=LOGGER_FORMAT,
        datefmt=LOGGER_DATE_FORMAT,
        style="$",
        handlers=[streamhandler])
    _LOGGER.info("Setup Logging")
    args = parse_arguments()
    updated_integration_index = create_integration_index(args.dev)
    updated_platform_index = create_platform_index(args.dev)
    index = {
        "inkBoard": inkBoard.__version__,
        "PythonScreenStackManager": PythonScreenStackManager.__version__,
        "inkBoarddesigner": inkBoarddesigner.__version__,
        "timestamp": dt.now().isoformat(),

        ##For these indexes, maybe consider adding more file info?
        ##Think timestamp, file size etc.
        ##Also, maybe put both zips in their own folder, 
        ##or make a folder for main/dev versions
        ##For folder structure -> folder per integration/platform with main/dev
        ##Then if required, older versions van also be kept in the repo i.e.

        ##Also, start raising errors when versions on the main branch are typed as a dev version (i.e. have 3 '.'s)
        ##Maybe don't let the entire workflow fail but make it show a warning/error?
        "platforms": updated_platform_index,
        "integrations": updated_integration_index
        }

    print(index)

    ##May actually put this in a different repo;
    ##inkBoard-index or something
    ##Which would hold all the zip files too
    ##Would have to see if that is allowed per github rules but it seems so
    ##If so, generated zip files should be compressed.

    with open(INDEX_FILE, "w") as file:
        json.dump(index,file,indent=4)
    print(f"Index dumped to {INDEX_FILE}")

if __name__ == "__main__":
    
    print("indexer running")
    main()

