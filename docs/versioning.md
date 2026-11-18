# Versions and compatibility

Python `openeight` and TypeScript `@openeight/sdk` are versioned independently.
A cookbook-only change does not cause an SDK release. Applications and cookbook
users install published SDK releases from PyPI or npm.

## Initial releases

Versions `0.0.x` are experimental. Any version change may require application
changes, including a patch-number increment. Pin the exact SDK version you have
qualified, review release notes before upgrading, and test the upgrade with your
application. Do not infer compatibility from a successful dependency installation.
This follows the initial-development meaning of major version zero in
[Semantic Versioning](https://semver.org/spec/v2.0.0.html#spec-item-4).

Maintainers must describe public API changes, required backend behavior, dependency
and runtime support changes, and migration steps in release notes. Python also
maintains a [changelog](../sdk/python/CHANGELOG.md). A future stable compatibility
policy will be documented before a 1.0 release; no long-term support or backport
commitment is implied by the current version numbers.

## What to qualify together

Use the package's public exports and documented interfaces. Private implementation
modules are not extension points. Framework integrations require a qualified
runtime and dependency combination; see the
[Python compatibility guide](../sdk/python/constraints/README.md).
Read each recipe's setup instructions and dependency declarations when pairing it
with an SDK release. TypeScript runtime and consumer checks are described in
[release procedures](../RELEASING.md).

For applications with durable state, keep pending work on its original supported
revision and configuration until completed or explicitly reconciled. An SDK upgrade
or downgrade does not authorize state migration or replay of business effects.
Release notes must identify any recovery compatibility constraints.
