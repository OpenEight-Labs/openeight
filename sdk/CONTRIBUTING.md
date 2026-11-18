# SDK development

Follow the repository-wide [contribution process](../CONTRIBUTING.md).
Run the commands below from `sdk/`; see [SDK setup](README.md) and the applicable
package README for dependencies and test commands.

## Checks

Run focused tests for changed behavior, the relevant SDK suite and package checks
when metadata changes. Check the agent-only publication boundary and generated
error mappings:

```bash
python scripts/check_public_surface.py
python scripts/generate_error_catalog.py --check
```

For Python compatibility registry changes, also run:

```bash
python scripts/check_framework_registry.py
```

Use explicit external qualification inputs for backend-dependent tests. Framework
support requires the matching qualified dependency closure, not only a successful
installation. Test the installed distribution for release-affecting packaging
changes. Python and TypeScript package setup and versioning remain independent.
