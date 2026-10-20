# Releasing

Two version numbers, and they move for different reasons. Mixing them misstates one of the two.

- **Package version**: the code. The engine, the bindings, the conformance suite.
- **Extension version**: the normative text. What an implementation must do to be answerable as
  conforming.

They are not required to match and they do not today: the text is at 1.1.0, the package is at
0.1.0. The text is at 1.1.0 because clause 13 arrived additively; the package will reach 1.0.0
when the implementation claims to implement the text completely, which it does not yet, because
the issuance side of claim levels waits on counsel.

## Cutting a package release

1. Green everything:

```
python -m pytest tests -q
python -m arli_engine.serve --port-a 8841 --port-b 8842 &
python -m arli_engine.conformance --target http://127.0.0.1:8841 --holder http://127.0.0.1:8842
```

2. Bump `__version__` in `arli_engine/__init__.py` and `version` in `pyproject.toml`. A test
   fails if the two disagree.
3. Add a `CHANGELOG.md` section with what changed and, in the same entry, what is still limited.
   The existing entries are the shape to follow: limits are stated, not implied.
4. Build and look inside before sending anywhere:

```
python -m pip wheel . -w /tmp/wheels --no-deps
python -c "import zipfile,glob;[print(n) for n in zipfile.ZipFile(sorted(glob.glob('/tmp/wheels/*.whl'))[-1]).namelist()]"
```

The wheel must carry `LICENSE`, `NOTICE`, the plugin manifest and the four console scripts.
5. Install it in a clean environment and run the suite from there. A green run in the source
   tree says nothing about what a user receives.
6. Tag `v<package version>` and push the tag.

## Publishing it

Not done, and held by decision rather than by omission: sending a wheel to a public package
index is a release to the world, and the export-control question about shipping an implementation
of ECDH and blind RSA signatures is still in front of counsel. The text and the code are
readable, which is a different act from shipping a package.

## The text

Never edit the text in this repository: it lives in `arli-discovery`, and a check in this suite
fails if a copy appears here. An additive change bumps the minor version, an incompatible one the
major, and `spec/CHANGELOG.md` in the text repository records what is deprecated.
