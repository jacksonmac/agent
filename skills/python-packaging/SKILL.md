---
name: python-packaging
description: lay out an installable Python package with pyproject.toml and verify with pip
---
To turn Python code into an installable package:

1. **Choose the layout.** Default to the src layout:

       pyproject.toml
       src/<package_name>/__init__.py
       src/<package_name>/...modules...
       tests/

   Use a flat layout (package dir at the root) only when the project already
   has one — do not mix the two.
2. **Write a minimal pyproject.toml** with `write_file`:

       [build-system]
       requires = ["setuptools>=68"]
       build-backend = "setuptools.build_meta"

       [project]
       name = "<package-name>"
       version = "0.1.0"
       description = "<one line>"
       requires-python = ">=3.10"
       dependencies = []

       [tool.setuptools.packages.find]
       where = ["src"]

   Add real runtime dependencies to `dependencies` — never pin what you
   don't import.
3. **Entry points** (only if the package has a CLI):

       [project.scripts]
       <command> = "<package_name>.<module>:main"

4. **Verify it installs.** Run `run_shell` with
   `pip install -e . --quiet` and check for a zero exit.
5. **Verify it imports.** Use `run_python` to run
   `import <package_name>; print(<package_name>.__name__)` — an editable
   install that can't be imported is not done.
6. **Keep tests outside the package** (top-level `tests/`), so they are not
   installed with the distribution.
