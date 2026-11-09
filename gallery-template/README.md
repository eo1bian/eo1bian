# EO1 gallery

Pictures and videos for an Electric Objects EO1 running EO1bian. Keep this repository **private** if the
pictures are personal.

1. Put pictures in `images/` and videos or GIFs in `videos/` (subfolders are fine; files under 100 MB).
2. Push. The workflow **Build the EO1 gallery** makes frame-ready copies on the branch `eo1`: pictures that
   fit the panel exactly, videos the frame's hardware decoder plays directly, and a `manifest.json`.
3. On the frame, `eo1.txt` gets `github_repo=<you>/<this repo>` and `github_token=<fine-grained token with
   read access to this repository's contents>`. The frame checks every minute and downloads only what is
   new, once, into its own storage.

`gallery.json` sets seconds per picture and per video, shuffle, the cross-fade, titles
(`"titles": {"images/sea.jpg": "The sea"}`; `""` = no title) and per-file durations
(`"durations": {"videos/waves.mp4": 120}`).

**Albums:** put files in subfolders (`images/Holiday 2024/`, `videos/EO1 - Original/`) and choose which ones
the frame shows with `"albums": ["EO1 - Original"]` in `gallery.json`. Leave it out (or empty) to show everything.

**Size:** the EO1 keeps the gallery on a 512 MB partition, and a GitHub gallery takes about twice its size there (a packed copy plus the files), so keep the built gallery under about 200 MB (the build log prints the total). The control page shows the free space.

Removing a file here removes it from the frame at its next check. To keep a file here but not show it, list it
in `gallery.json` under `"hidden": ["images/old.jpg"]`. You can also hide pictures from the frame's control page
on your phone ("Hide this"); that is kept on the frame and can be undone there.

**Remote control:** `control.json` (optional) sets the screen, brightness, schedule, time per picture, titles and
what goes around pictures from here; the EO1 follows each change at its next check. The EO1 Remote page edits it
for you from a phone. See the EO1bian README, "Control it from anywhere".
