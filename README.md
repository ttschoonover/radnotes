# RadNotes

Minimal markdown notes that sync between your own devices, and with friends,
peer to peer over [Radicle](https://radicle.xyz). No account, no company
server: your notes are plain `.md` files in `~/RadNotes`.

Type `radnotes` and your notes open in the browser. The first line of a note is
its title, and everything saves as you type.

## Install

On macOS or Linux (including a Raspberry Pi):

```sh
curl -fsSL https://raw.githubusercontent.com/ttschoonover/radnotes/main/install.sh | sh
```

The installer sets up RadNotes and Radicle. Then open a new terminal and run,
once per device:

```sh
rad auth          # create your Radicle identity
rad node start    # start syncing (again after each restart)
radnotes          # open your notes
```

Requirements: Python 3.10 or newer and git. The installer tells you how to get
them if they're missing. Update later with `radnotes update`.

## Sync your own devices

On the device you already use, open **Share** and click **Show a code**, or run:

```sh
radnotes pair
```

It shows a code like `veggiepi-482913`. On the other device, enter it under
**Share → Have a code?**, or run:

```sh
radnotes pair veggiepi-482913
```

That's all. Notes already on the new device are kept and added in.

The two devices talk to each other directly, so they need to reach each other:
on the same Wi-Fi, or anywhere using [Tailscale](https://tailscale.com) (free;
install it on both and sign in to the same account). A device that's always on,
like a Raspberry Pi at home, keeps everything in sync while your laptop sleeps.

## Share with friends

```sh
radnotes share "Book club"
```

This makes a shared space, separate from your own notes, and prints an invite
code like `rn-z3gqcJUoA1n9HaHKufZs5FCSGazv5`. Friends paste it under
**Share → Have a code?** or run `radnotes join rn-z…`. It shows up as its own
list in the app, and everyone's notes in it merge automatically.

Shared spaces travel through Radicle's public network, so friends can be
anywhere and don't need Tailscale. The trade-off: **anyone who has the code can
read and write the space.** Keep codes among the people you mean to share with,
and keep private things in your own notes, which are only ever sent to your own
paired devices.

## From the terminal

```sh
radnotes new "call the bank"     # jot something down
radnotes new                     # write a longer note in your editor
radnotes ls                      # list notes, newest first
radnotes show 2                  # print note 2
radnotes edit 2
radnotes rm 2
radnotes sync
radnotes spaces                  # your shared spaces and their codes
radnotes ls -s "Book club"       # -s works with new, ls, show, edit and rm
radnotes status
```

## If two people edit the same note

Nothing is lost. Your version stays in the note, and the other version is saved
as a separate note next to it. Combine them and delete the extra.

## Privacy

- Your own notes are a private Radicle repository: only devices you've paired
  can fetch them.
- Pairing codes work once, for 10 minutes, and are cancelled after 5 wrong tries.
- The app answers only on your own device (127.0.0.1) and needs a key that
  changes every launch, so websites can't read your notes.

## Developing

```sh
git clone https://github.com/ttschoonover/radnotes && cd radnotes
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
.venv/bin/radnotes --mock    # use an offline stand-in for the Radicle network
```

| File | What it does |
|---|---|
| `radnotes/notes.py` | notes, shared spaces, syncing |
| `radnotes/pairing.py` | short device codes |
| `radnotes/radicle.py` | every `rad` and `git` call, plus the offline stand-in |
| `radnotes/cli.py` | the `radnotes` command |
| `radnotes/server.py`, `static/` | the app (Pico CSS, no build step) |

To publish a release, bump `__version__` in `radnotes/__init__.py`, then
`git tag v0.4.0 && git push --tags`. GitHub builds it, and the installer and
`radnotes update` pick it up.

## License

Copyright (c) 2026 Timothy Schoonover. All rights reserved. You may download and
use RadNotes; see [LICENSE](LICENSE) for what you may not do.
