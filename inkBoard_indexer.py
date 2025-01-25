"""Script to automatically create packages of all integrations and platforms present in the designer and track their versions

Meant to be used with github workflows
"""

import os
import json
import logging
from pathlib import Path
import shutil
import tempfile
import zipfile
import argparse

import inkBoard
from inkBoard import constants
from inkBoard.types import manifestjson, platformjson
from inkBoard.packaging import ZIP_COMPRESSION, ZIP_COMPRESSION_LEVEL, parse_version

import inkBoarddesigner
import PythonScreenStackManager

print("Successfully imported everything")

_LOGGER = inkBoard.getLogger("inkBoard-index")
_LOGGER.setLevel(logging.INFO)

INDEX_FOLDER = Path(__file__).parent
INDEX_FILE = INDEX_FOLDER / "index.json"
INTEGRATION_INDEX_FOLDER = INDEX_FOLDER / "integrations"
PLATFORM_INDEX_FOLDER = INDEX_FOLDER / "platforms"

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
        "platforms": {},
        "integrations": {"api": {"main": "1.0.0"}},
        }

integration_index = current_index["integrations"].copy()
platform_index = current_index["platforms"].copy()

def parse_arguments():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--dev', action='store_true', dest='dev',
                        help="Assumed zip packages created are dev packages, and appends _dev to the packages")
    return parser.parse_args()

def gather_folders(base_folder) -> list[Path]:
    folders = []

    for p in Path(base_folder).iterdir():
        if p.is_dir(): folders.append(p)
    return folders

def create_integration_index(dev_mode: bool):
    folder = constants.DESIGNER_FOLDER / "integrations"
    int_folders = gather_folders(folder)
    for p in int_folders:
        manifest_file = p / "manifest.json"
        if not manifest_file.exists():
            continue

        if dev_mode:
            key = "dev"
            package_name = INTEGRATION_INDEX_FOLDER / f"{p.name}_dev.zip"
        else:
            key = "main"
            package_name = INTEGRATION_INDEX_FOLDER / f"{p.name}.zip"

        with open(manifest_file) as file:
            d = manifestjson(**json.load(file))
        
        if p.name in integration_index:
            integration_index[p.name][key] = d["version"]
            
        else:
            integration_index[p.name] = {key: d["version"]}

        if (not (package_name).exists()
                or p.name not in current_index["integrations"]
                or parse_version(d["version"]) > parse_version(current_index["integrations"][p.name].get(key, "0.0.0"))):
            create_integration_zip(p, package_name)
            # pass
    return integration_index

def create_platform_index(dev_mode: bool):
    folder = constants.DESIGNER_FOLDER / "platforms"
    plt_folders = gather_folders(folder)
    for p in plt_folders:
        platform_file = p / "platform.json"
        if not platform_file.exists():
            continue

        if dev_mode:
            key = "dev"
            package_name = PLATFORM_INDEX_FOLDER / f"{p.name}_dev.zip"
        else:
            key = "main"
            package_name = PLATFORM_INDEX_FOLDER / f"{p.name}.zip"


        with open(platform_file) as file:
            d = platformjson(**json.load(file))
            platform_index[p.name] = d["version"]
        
        if (not (package_name).exists()
                or p.name not in current_index["platforms"]
                or parse_version(d["version"]) > parse_version(current_index["platforms"][p.name].get(key, "0.0.0"))):
            create_platform_zip(p, package_name)
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
        print(f"Gathering integration {name}")
        shutil.copytree(
            src = integration_folder,
            dst= Path(tempdir) / name,
            ignore=lambda *args: ("__pycache__","emulator.json", "designer", "designer.py")
        )

        print(f"Zipping up integration {name}")
        with zipfile.ZipFile(zip_file_path, 'w', ZIP_COMPRESSION, compresslevel=ZIP_COMPRESSION_LEVEL) as zip_file:
            for foldername, subfolders, filenames in os.walk(tempdir):
                _LOGGER.verbose(f"Zipping contents of folder {foldername}")
                for filename in filenames:
                    file_path = os.path.join(foldername, filename)
                    zip_file.write(file_path, os.path.relpath(file_path, tempdir))
                for dir in subfolders:
                    dir_path = os.path.join(foldername, dir)
                    zip_file.write(dir_path, os.path.relpath(dir_path, tempdir))
        print(f"Succesfully packaged integration {name}")
    return

def create_platform_zip(platform_folder: Path, zip_file_path: Path):
    
    with tempfile.TemporaryDirectory(dir=str(INTEGRATION_INDEX_FOLDER)) as tempdir:
        name = platform_folder.name
        print(f"Gathering platform {name}")
        shutil.copytree(
            src = platform_folder,
            dst= Path(tempdir) / name,
            ignore=lambda *args: ("__pycache__","designer.py", "designer")
        )

        print(f"Zipping up platform {name}")
        with zipfile.ZipFile(zip_file_path, 'w', ZIP_COMPRESSION, compresslevel=ZIP_COMPRESSION_LEVEL) as zip_file:
            for foldername, subfolders, filenames in os.walk(tempdir):
                _LOGGER.verbose(f"Zipping contents of folder {foldername}")
                for filename in filenames:
                    file_path = os.path.join(foldername, filename)
                    zip_file.write(file_path, os.path.relpath(file_path, tempdir))
                for dir in subfolders:
                    dir_path = os.path.join(foldername, dir)
                    zip_file.write(dir_path, os.path.relpath(dir_path, tempdir))
        print(f"Succesfully packaged platform {name}")
    return

if __name__ == "__main__":
    
    print("indexer running")

    args = parse_arguments()
    index = {
        "inkBoard": inkBoard.__version__,
        "PythonScreenStackManager": PythonScreenStackManager.__version__,
        "inkBoarddesigner": inkBoarddesigner.__version__,
        "platforms": create_platform_index(args.dev),
        "integrations": create_integration_index(args.dev)
        }

    print(index)
    

    ##May actually put this in a different repo;
    ##inkBoard-index or something
    ##Which would hold all the zip files too
    ##Would have to see if that is allowed per github rules but it seems so
    ##If so, generated zip files should be compressed.
    # print(index)
    with open(Path(__file__).parent / "index.json", "w") as file:
        json.dump(index,file,indent=4)