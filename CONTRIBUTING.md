# Contributing to dbus-shelly

Contributions are welcome. These guidelines keep them easy to review and merge. By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE) of this repository.

## Before you start

Open an issue in [victronenergy/venus](https://github.com/victronenergy/venus/issues), with a title that starts with `dbus-shelly: `, before you start on new device or component support, or on a larger change, and wait for the maintainer to respond. Some devices and components are already planned, and others we won't support or want to support differently. Agreeing on the approach first means you don't spend time (or AI tokens) on work that won't be merged.

Discuss these in the issue first as well:

- **New or upgraded dependencies.** The driver runs on the Python packages Venus OS ships, which can be older than the latest release on PyPI (check with `opkg list-installed | grep python3-` on a GX running the latest Venus OS; pip is not installed by default). A new package or version needs a change to Venus OS itself.
- **New D-Bus paths, service types or settings.** These are used by the GUI, VRM and other Venus OS services, so they must follow the [Venus D-Bus API](https://github.com/victronenergy/venus/wiki/dbus) and not be specific to one device.

## Issues and pull requests

- **Describe the problem in an issue.** Open it in [victronenergy/venus](https://github.com/victronenergy/venus/issues), not in this repository, with a title that starts with `dbus-shelly: `. The issue holds the problem description: what goes wrong, on which device and firmware, and how to reproduce it.
- **The pull request holds the solution.** Reference the issue by its full URL (`#123` would point to this repository) and describe briefly what the change does and how it was tested. There is no need to repeat the problem.
- **Don't close the issue.** Don't use `Fixes`, `Closes` or similar in commits or PRs, and don't close the issue yourself. The Venus OS maintainer closes it when the fix is included in a Venus OS release. This is how we track when a change ships and put it in the changelog.
- **One topic per pull request.** Keep unrelated fixes and refactoring out of it.

## Branches and commits

- Regular contributors get write access to the repository. Work on a feature branch in this repository instead of a fork, so the maintainer can push small changes to the branch directly. Name the branch `<name>/<feature>`, for example `rein/plus-1pm-support`. Never push to `master`.
- Rebase on `master` instead of merging `master` into your branch.
- Make each commit one logical change. Squash fixup commits before asking for review.
- Write a short commit message: a title in the imperative ("Add support for ..."), and a body only when the why isn't obvious.
- When AI helped write a commit, add a trailer `Assisted-by: <agent>:<model>`, such as `Assisted-by: Claude Code:claude-opus-5-5`.

## Keep it short

Using AI is fine, but the result must be easy to review. Keep issues, pull requests and commit messages short and to the point. Leave out long explanations, repeated context and generic disclaimers.

## Code

- Follow the existing style and patterns: tabs for indentation, asyncio throughout. Don't reformat or refactor code that is not part of your change.
- New source files go in `FILES` in the `Makefile`, or they won't be installed.
- When you add support for a device or component, update the component table and the list of tested devices in `README.md`.
- Don't break devices that run older Shelly firmware.

## Testing

- Run the end-to-end tests (`make test`, see `tests/`) and add tests for new behaviour. CI must pass.
- Test on a real GX device with real Shelly hardware as well. Mention the GX, Venus OS version, Shelly device and Shelly firmware version you tested with in the pull request. If the change depends on the firmware version, mention which versions you checked.
