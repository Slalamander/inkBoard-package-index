"""Script to automatically create packages of all integrations and platforms present in the designer and track their versions

Meant to be used with github workflows
"""
from typing import Generator, get_args
import os
import json
import logging
from pathlib import Path
import shutil
import tempfile
import zipfile
import argparse
import sys
import subprocess

from datetime import datetime as dt

import inkBoard
from inkBoard.logging import ColorFormatter, LOG_LEVELS
from inkBoard import constants
from inkBoard.packaging.constants import ZIP_COMPRESSION, ZIP_COMPRESSION_LEVEL
from inkBoard.packaging.version import parse_version, write_version_filename

from inkBoard.packaging.types import (
    PackageIndex,
    manifestjson,
    platformjson,
    indexpackagedict, 
    branchtypes,
    )

import inkBoarddesigner
import PythonScreenStackManager

DEBUGGING = bool(sys.gettrace())

_LOGGER = inkBoard.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

LOGGER_FORMAT = '${asctime} [${levelname}]: ${message}'
LOGGER_DATE_FORMAT = '%H:%M:%S'

INDEX_FOLDER = Path(__file__).parent
if DEBUGGING:
    INDEX_FOLDER = INDEX_FOLDER / "debug_index"

INDEX_FILE = INDEX_FOLDER / "index.json"

INTEGRATION_INDEX_FOLDER = INDEX_FOLDER / "integrations"
PLATFORM_INDEX_FOLDER = INDEX_FOLDER / "platforms"
ARCHIVE_FOLDER_STR = "versions"

DEV_PATTERN = r"([0-9.]+)_dev.zip"
MAIN_PATTERN = r"([0-9.]+).zip"

LOG_LEVELS = list(LOG_LEVELS)
LOG_LEVELS.remove("VERBOSE")

class EXITCODES:
    NONE = 0
    "Nothing went wrong (exit code 0)"

    INTEGRATIONINDEXERROR = 11
    "Error creating the integration index"

    PLATFORMINDEXERROR = 12
    "Error creating the platform index"

    BOTHINDEXERROR = 13
    "Error creating both the integration and platform index"

if INDEX_FILE.exists():
    with open(INDEX_FILE, "r") as file:
        current_index : PackageIndex = json.load(file)
else:
    current_index : PackageIndex = {
        "inkBoard": {}, #inkBoard.__version__,
        "PythonScreenStackManager": {}, #PythonScreenStackManager.__version__,
        "inkBoarddesigner": {}, #inkBoarddesigner.__version__,
        "timestamp": {}, #dt.fromtimestamp(0).isoformat(),
        "platforms": {},
        "integrations": {},
        }

integration_index : dict[str,dict[str, indexpackagedict]] = current_index["integrations"].copy()
platform_index : dict[str,dict[str, indexpackagedict]] = current_index["platforms"].copy()

class inkBoardIndexingError(Exception):
    "Base exception for errors in the indexing process"
    def __init__(self, *args, errors : dict = None):
        super().__init__(*args)
        if errors:
            self._errors = errors

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
    parser.add_argument('--commit', action='store_true', dest='commit',
                    help="Commit changes while running the script",
                    default=False)
    parser.add_argument('--branch', dest="branch", choices=get_args(branchtypes),
                        help="The branch to push the changes to. Ignored if --commit is not passed", default=None)
    parser.add_argument('--logs', dest="log_level", choices=LOG_LEVELS,
                        help="The logging level to use", default="INFO")
    return parser.parse_args()

def gather_folders(base_folder) -> Generator[Path, None, None]:
    """Gathers all folders in the base_folder, provided they do not start with an `_`"""

    for p in Path(base_folder).iterdir():
        if p.is_dir() and not p.name.startswith("_"):
            yield p

def add_and_push_commit(add_path : str, message : str):
    """Pushes a commit
    """

    _LOGGER.info(f"Pushing a commit {add_path}: {message}")
    subprocess.run(["git", "add", INDEX_FOLDER], check=True, stdout=subprocess.PIPE).stdout
    subprocess.run(["git", "commit", "-m", message], check=True, stdout=subprocess.PIPE).stdout

def create_integration_index(dev_mode: bool, commit_changes : bool):

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
            package_dict = integration_index[p.name].get(branch, {"version": "0.0.0"})
            index_version = parse_version(package_dict["version"])
            
        else:
            index_version = parse_version("0.0.0")
            # integration_index[p.name] = {branch: d["version"]}
            integration_index[p.name] = {}

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
                package_name =  index_folder / write_version_filename(p.name, manifest_version, "_dev.zip")
            else:
                package_name =  index_folder / write_version_filename(p.name, manifest_version)

        elif dev_mode:
            make_package = True
            package_name =  index_folder / write_version_filename(p.name, manifest_version, "_dev.zip")
            old_package =  index_folder / write_version_filename(p.name, index_version, "_dev.zip")
            if old_package.exists() and index_version.is_prerelease:
                ##Do not want to archive mainline versions which do not come from the main branch
                archive_old_package = True
        else:
            make_package = True
            package_name =  index_folder / write_version_filename(p.name, manifest_version)
            old_package =  index_folder / write_version_filename(p.name, index_version)
            if old_package.exists():
                archive_old_package = True

        if archive_old_package:
            archive_package = index_folder / ARCHIVE_FOLDER_STR / write_version_filename(p.name, index_version)
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
            integration_index[p.name].pop(branch, None)

        if len(list(index_folder.glob("*.zip"))) > 1:
            ##Check to see if the current folder structure is ok to make a new package in
            msg = f"There are two or more packages in the main folder {index_folder} of {pack_type} {p.name} now, will not create new {pack_type} package {package_name.name}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        if make_package:
            integration_index[p.name][branch] = create_integration_zip(p, package_name)
            integration_index[p.name][branch]["version"] = d["version"]
            if commit_changes:
                add_and_push_commit(str(index_folder), f"Packaged {pack_type} {p.name} version {manifest_version}")

    if err_dict:
        d = {}
        for k, v in err_dict.items():
            d.setdefault(v, 0)
            d[v] += 1
        msg = f"Errors while creating {pack_type} index: {d}. See logs for more details"
        raise inkBoardIndexingError(msg, errors=err_dict)
    return integration_index

def create_platform_index(dev_mode: bool, commit_changes : bool):
    pack_type = "platform"
    folder = constants.DESIGNER_FOLDER / "platforms"
    int_folders = gather_folders(folder)
    err_dict = {}
    for p in int_folders:
        platform_file = p / "platform.json"
        if not platform_file.exists():
            msg = f"No platform file for {pack_type} folder {p}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        branch = "dev" if dev_mode else "main"
        with open(platform_file) as file:
            d = platformjson(**json.load(file))

        index_folder = PLATFORM_INDEX_FOLDER / p.name
        old_package = None
        make_package = False
        archive_old_package = False

        platform_version = parse_version(d["version"])
        if p.name in platform_index:
            package_dict = platform_index[p.name].get(branch, {"version": "0.0.0"})
            index_version = parse_version(package_dict["version"])
            
        else:
            index_version = parse_version("0.0.0")
            platform_index[p.name] = {}

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
                package_name =  index_folder / write_version_filename(p.name, platform_version, "_dev.zip")
            else:
                package_name =  index_folder / write_version_filename(p.name, platform_version)

        elif dev_mode:
            make_package = True
            package_name =  index_folder / write_version_filename(p.name, platform_version, "_dev.zip")
            old_package =  index_folder / write_version_filename(p.name, index_version, "_dev.zip")
            if old_package.exists() and index_version.is_prerelease:
                ##Do not want to archive mainline versions which do not come from the main branch
                archive_old_package = True
        else:
            make_package = True
            package_name =  index_folder / write_version_filename(p.name, platform_version)
            old_package =  index_folder / write_version_filename(p.name, index_version)
            if old_package.exists():
                archive_old_package = True

        if archive_old_package:
            archive_package = index_folder / ARCHIVE_FOLDER_STR / write_version_filename(p.name, index_version)
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
            platform_index[p.name].pop(branch,None)

        if len(list(index_folder.glob("*.zip"))) > 1:
            ##Check to see if the current folder structure is ok to make a new package in
            msg = f"There are two or more packages in the main folder {index_folder} of {pack_type} {p.name} now, will not create new {pack_type} package {package_name.name}"
            _LOGGER.error(msg)
            err_dict[p.name] = FileIndexError(msg)
            continue

        if make_package:
            platform_index[p.name][branch] = create_platform_zip(p, package_name)
            platform_index[p.name][branch]["version"] = d["version"]
            if commit_changes:
                add_and_push_commit(str(index_folder), f"Packaged {pack_type} {p.name} version {platform_version}")


    if err_dict:
        d = {}
        for k, v in err_dict.items():
            d.setdefault(v, 0)
            d[v] += 1
        msg = f"Errors while creating {pack_type} index: {d}. See logs for more details"
        raise inkBoardIndexingError(msg, errors=err_dict)
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
            ignore=lambda *args: ("__pycache__", "designer", "designer.py")
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
    return {}

def create_platform_zip(platform_folder: Path, zip_file_path: Path):
    
    with tempfile.TemporaryDirectory(dir=str(INTEGRATION_INDEX_FOLDER)) as tempdir:
        name = platform_folder.name
        _LOGGER.info(f"Gathering platform {name}")
        shutil.copytree(
            src = platform_folder,
            dst= Path(tempdir) / name,
            ignore=lambda *args: ("__pycache__", "emulator.json", "designer.py", "designer")
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
    return {}

def folder_setup():
    if DEBUGGING and not INDEX_FOLDER.exists():
        INDEX_FOLDER.mkdir()
        _LOGGER.info(f"Created index folder {INDEX_FOLDER}")
    
    if not INTEGRATION_INDEX_FOLDER.exists(): 
        INTEGRATION_INDEX_FOLDER.mkdir()
        _LOGGER.info(f"Created integration folder {INTEGRATION_INDEX_FOLDER}")

    if not PLATFORM_INDEX_FOLDER.exists():
        PLATFORM_INDEX_FOLDER.mkdir()
        _LOGGER.info(f"Created platform folder {PLATFORM_INDEX_FOLDER}")

def main():
    exceptions : dict[str, inkBoardIndexingError] = {}
    args = parse_arguments()

    streamhandler = logging.StreamHandler()
    streamhandler.setFormatter(ColorFormatter(LOGGER_FORMAT, LOGGER_DATE_FORMAT))
    streamhandler.setLevel(args.log_level)
    logging.basicConfig(
        format=LOGGER_FORMAT,
        datefmt=LOGGER_DATE_FORMAT,
        style="$",
        handlers=[streamhandler])

    if args.commit and args.branch is None:
        raise argparse.ArgumentError(None, "When supplying --commit, branch must be specified")

    if DEBUGGING:
        msg = "Indexer running in DEBUG mode"    
        if args.dev:
            msg = msg + "and DEV_MODE"
        branch_str = "DEBUG"
    elif args.dev:
        msg = "Indexer running in dev mode"
        branch_str = "dev"
    else:
        msg = "Indexer running in main mode"
        branch_str = "main"
    _LOGGER.info(msg)

    folder_setup()

    try:
        updated_integration_index = create_integration_index(args.dev, args.commit)
    except inkBoardIndexingError as exce:
        updated_integration_index = integration_index
        exit_code = EXITCODES.INTEGRATIONINDEXERROR
        exceptions["integrations"] = exce

    try:
        updated_platform_index = create_platform_index(args.dev, args.commit)
    except inkBoardIndexingError as exce:
        if exit_code is EXITCODES.NONE:
            exit_code = EXITCODES.PLATFORMINDEXERROR
        else:
            exit_code = EXITCODES.BOTHINDEXERROR
        updated_platform_index = platform_index
        exceptions["platforms"] = exce
    
    #[x]: from the messages: want to know which platforms/integrations errored.
    index = {
        "inkBoard": {branch_str: inkBoard.__version__},
        "PythonScreenStackManager": {branch_str: PythonScreenStackManager.__version__},
        "inkBoarddesigner": {branch_str: inkBoarddesigner.__version__},
        "timestamp": {branch_str: dt.now().isoformat()},

        ##For these indexes, maybe consider adding more file info?
        ##Think timestamp, file size etc. Can add these later. For now, make extensible by giving version a key
        #[x]: put versions in a seperate key

        "platforms": updated_platform_index,
        "integrations": updated_integration_index
        }

    _LOGGER.debug(f"New index is: {index}")

    with open(INDEX_FILE, "w") as file:
        json.dump(index,file,indent=4)
    _LOGGER.info(f"Index dumped to {INDEX_FILE}")

    #[x] Probably do push/pull in here too instead of the workflow -> that way the exit code can be set without it causing issues, and hopefully it reflects in the workflow
    #[x] Implement the final commit/push code
    #[x] add argument for the running branch
    #[x] Set exit code according to output of packagers -> somewhat done. Simply returning 1 on error and using logs
    #[ ] Let the zipfile create functions return dicts with info
    #[ ] Add a check to ensure every integration/platform has a unique name (i.e. no platform and integration may have the same name)
    #[ ] Trigger workflows op pull-requests and releases (https://medium.com/hostspaceng/triggering-workflows-in-another-repository-with-github-actions-4f581f8e0ceb)

    if args.commit:
        add_and_push_commit(".", f"Updated {'dev' if args.dev else 'main'} index")
        _LOGGER.info("Fetching repo and pushing changes")
        subprocess.run(["git", "fetch", "origin", args.branch], check=True, stdout=subprocess.PIPE).stdout
        subprocess.run(["git", "push", "origin", args.branch], check=True, stdout=subprocess.PIPE).stdout

    if not exceptions:
        _LOGGER.info("Indexer ran succesfully")
        return 0
    else:
        msg = "INDEXER FAILED\nThe following errors were collected:\n\n"
        c_size = min(80,shutil.get_terminal_size().columns)
        sep_bar = "="*c_size
        sep_bar_nl = sep_bar + "\n"
        print("\n" + sep_bar_nl)
        for pack_type, exces in exceptions.items():
            msg = msg + sep_bar_nl + pack_type.capitalize() + "\n" + sep_bar_nl

            for package, exce in exces._errors.items():
                exce : Exception
                msg = msg + f"- {package}: {type(exce).__name__}\n    " + f"{exce}\n\n"

        _LOGGER.critical(msg)
        return 1

if __name__ == "__main__":
    
    print("indexer running")
    sys.exit(main())

