"""Puts the repository root on sys.path so tests can `from pipeline import ...`.

pytest inserts the directory containing the topmost conftest.py, so this file's
existence at the root is the whole mechanism. Avoids needing an installed package
for a repo that is not distributed.
"""
