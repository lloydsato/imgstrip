# Releasing a version, and archiving it on Zenodo

Each released version gets its own DOI on Zenodo, and all versions share one *concept* DOI that always resolves to the
newest. Zenodo takes the title, authors, version and licence from `CITATION.cff`.

## Once: connect the repository to Zenodo

1. Sign in at <https://zenodo.org> with your GitHub account.
2. Under *GitHub* in your Zenodo account settings, switch the `lloydsato/imgstrip` repository **on**. From then on,
   every *published* GitHub release is archived automatically.

## Archive 1.2.4 (once, without touching its tag or release)

1.2.4 was released before the repository was connected, so Zenodo has not seen it. Do not re-publish its GitHub
release: that would also re-run the PyPI workflow, which fails for a version PyPI already has.

1. Download the source archive of the `v1.2.4` tag (*Releases* → 1.2.4 → *Source code (zip)*).
2. After 1.2.5 is archived (below), open its Zenodo record, choose *New version*, upload that zip, set the version to
   1.2.4 and the publication date to 1.2.4's release date, and publish. Both versions then sit under the same concept
   DOI, each with its own.

## Every release (1.2.5 and later)

1. `__version__` in `src/imgstrip/__init__.py`, `version` in `CITATION.cff` and the top entry of `CHANGELOG.md` name the
   new version; `python scripts/render_readme.py`; the CI workflow is green on `main`.
2. Create a GitHub release with tag `vX.Y.Z` (exactly the version) and the changelog entry as its notes, and publish
   it. The *Publish to PyPI* workflow builds and uploads the package; Zenodo archives the tag and mints its DOI.
3. Add the DOIs to `CITATION.cff` (`doi:` the concept DOI; under `identifiers:` the version DOI) and, if you like, the
   Zenodo badge to the README template, in a follow-up commit. They do not change the released files.
