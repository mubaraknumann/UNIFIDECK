"""Metadata sub-package — title normalisation + Metacritic lookups.

OP-28 | py_modules/unifideck/metadata/__init__.py

Two modules:

* ``unifidb``    — title normalisation for cross-store
  matching (strip TM/®, edition suffixes, punctuation,
  diacritics). Used by ``cross_store_dedup`` to decide
  whether two titles refer to the same game.
* ``metacritic`` — fetch + cache Metacritic / OpenCritic
  review scores for a title.

The package name "metadata" is the catch-all for "everything
about a game that isn't a launch artifact" — release date,
genres, scores, store-cross-references.
"""
