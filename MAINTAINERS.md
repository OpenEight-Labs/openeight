# Maintainers and decisions

OpenEight Systems Private Limited maintains this repository. The current code
owner is [@vasu-openeight](https://github.com/vasu-openeight), as recorded in
[CODEOWNERS](.github/CODEOWNERS), and coordinates SDK, cookbook and release decisions.

Discuss proposed changes in an issue or pull request so contributors can see the
problem, alternatives and decision. Maintainers decide what to accept based on
product fit, public API compatibility, security and the cost of ongoing support.
When declining a proposal, explain the reason and any narrower change that would
be useful. Contributors can ask for reconsideration on the same thread with new
evidence or a revised proposal.

## Solo-maintainer review

The project currently has one maintainer. Changes go through focused pull requests
with recorded verification and resolved review conversations. The maintainer may
merge their own PR after reviewing the final diff and test results; an independent
approving review is not currently required. Community reviews are welcome, but do
not grant merge or publication authority. CODEOWNERS identifies ownership and
routes review requests; mandatory code-owner approval is disabled during solo
maintenance.

For public contracts, security-sensitive behavior and releases, record the affected
contracts, security implications, validation, skipped checks and rollback plan.
Seek independent review when available and identify whether the final review was
performed by the author or another reviewer. Automated checks and author review
must not be described as independent approval.

SDK publication requires a separate, explicit approval of the qualified artifacts.
The sole maintainer may initiate and approve a Python release through the protected
`pypi` environment; administrator bypass remains disabled. Starting CI or merging
a PR does not authorize publication.

When a second eligible maintainer joins, update CODEOWNERS and the GitHub rules to
require independent PR approval, dismiss stale approvals, review the latest push,
and prevent release self-approval. Repository access changes remain separate
administrative actions. These documents do not themselves enforce GitHub settings.

Reviews happen as capacity permits, without a guaranteed turnaround. Maintainers
should make outstanding questions and requested changes explicit. Contributors
retain credit through their commits and pull requests. Changes to maintainer
membership or project policy should be proposed and explained in a pull request;
repository access changes remain a separate administrative action.

Read [release procedures](RELEASING.md) and
[maintainer protections](sdk/MAINTAINERS.md) for operational controls.
[Security reports](SECURITY.md) and [conduct reports](CODE_OF_CONDUCT.md) use their
private reporting procedures rather than public decision threads.
