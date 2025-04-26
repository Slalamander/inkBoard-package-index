# inkBoard-package-index
This repository acts as an easy index to download inkBoard packages, without the need to extract them from the designer.
Integrations and platforms are automatically extracted and compressed, the zipfiles can be downloaded from this repository, or using ``inkBoard download`` when that is fully implemented.

It extracts both the current development version and main version of both, though keep in mind those may simply be the same files (if not updated, versions may be the same without the files being the exact same).
Indexing may change, i.e. appending versions to the package names.

Currently, the calls to the github api are limited, since there is no way to authorise to the api. This may change later, if required, although it may also be possible to keep the api calls to a minimum. Will come later when I manage to get back to fully fleshing out the implementation in inkBoard (the main problem being the limitation I set myself to not use any/as as possible external python libraries)
