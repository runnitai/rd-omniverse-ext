# RunDiffusion for NVIDIA Omniverse

Generate AI images from your viewport and add generated 3D assets directly to
your stage, all from a panel inside your Omniverse application.

RunDiffusion connects your scene to its creative tool catalogue so you can
capture a view, choose a tool, and generate while you continue working. Compare
images with your original capture, reuse results for another generation, or
bring a generated 3D asset into your scene. Your scene changes only when you
choose to make a change.

This is the source for the extension published to the Kit Community Registry. It
is currently in beta.

## What you need

- Windows, with an Omniverse Kit application on Kit 110.1.x
- A RunDiffusion account (the panel has a link to create one)
- Nothing else. There is no build step: a Kit extension is a folder.

## Install

**From your app's Extensions window.** Open **Window > Extensions** and search
for **RunDiffusion**. If community extensions are not listed, turn on
third-party extensions in that window's settings first. Install it, then switch
on **Autoload** in the same row so it comes back next time the app starts.

**From a release.** Download the zip from
[Releases](https://github.com/runnitai/rd-omniverse-ext/releases), unzip it, and
rename the folder inside to `rundiffusion.omniverse` if it carries a version
suffix. Move that folder into your app's `exts` folder, beside the extensions
already there, and start the app.

If you already installed a copy by hand, delete that folder before installing
from the registry. Two copies of the same extension is one copy too many.

## First run

The panel opens on a sign-in screen. Press **Sign in**, authorize the device in
the browser window that opens, and come back. The panel remembers the session,
sealed to your Windows user account, so you only do this once per machine.

## Support

Bug reports and feature requests are welcome as
[issues](https://github.com/runnitai/rd-omniverse-ext/issues). Please include
your Kit version, which application you are running, and the extension version
shown on the panel's Account tab.

For account, billing or anything not specific to this extension, go to
[rundiffusion.com](https://www.rundiffusion.com).

## About this repository

This is published source for a product built in a private repository. Changes
flow one way, out to here. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening
a pull request, and [SECURITY.md](SECURITY.md) to report a vulnerability.

Extensions in the Kit Community Registry are contributed by their developers
rather than by NVIDIA, and carry NVIDIA's "Sample" support level. Support for
this one comes from RunDiffusion.

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](LICENSE).

The RunDiffusion name and logo, including `data/rd-mark.png`, are not covered by
that license. One icon is from Material Design Icons under Apache-2.0: see
[ICONS-LICENSE.md](exts/rundiffusion.omniverse/data/ICONS-LICENSE.md).
