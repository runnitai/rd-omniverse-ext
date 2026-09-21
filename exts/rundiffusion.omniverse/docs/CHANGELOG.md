# Changelog

## [0.3.0] - 2026-09-14

### Added

- **The output size follows the view.** Capturing sets a tool's output size to
  the declared size closest to the viewport's shape, whether or not Fill
  Viewport is on. Pick a size yourself and it stays put.
- **A size picked by hand shapes the capture.** The viewfinder shows the crop
  and the capture is cropped to that shape, about its centre.
- **The prompt wraps and grows.** When you click away from a long prompt it
  wraps to the width of the box, which grows to fit and scrolls past about
  fourteen lines. A blank line is kept as a paragraph break.
- **Keep this view as a camera.** An optional box under Capture saves the
  framing as a camera under `/RunDiffusion/Cameras` on your stage, undoable in
  one step. A result captured from a kept or named camera offers **Go to its
  view**.
- **Step through Library and Uploads** in the image viewer with Previous and
  Next, or the left and right arrow keys.

### Changed

- **The viewfinder leads the Create tab.** A full-width live frame sits at the
  top, with where it is looking, whether it is live, what will be sent and the
  view's shape written on it. Click the frame or the button under it to
  capture. Capturing and any refusal are reported in the frame, not in the
  status line.
- **The frame folds to a one-line strip after the first capture**, showing the
  capture and offering Recapture and Show viewfinder. Show viewfinder keeps the
  frame open until Hide viewfinder. A panel narrower than 300px starts on the
  strip.
- A tool with two image fields asks which one the frame captures into.
- Tabs are an underlined strip instead of four raised buttons.
- The tool is a label with Browse... beside it rather than a full-width button.
- Field headings are small capitals. The image field states `N of 12` up front,
  and its help text shows only while it is empty.
- The output size heading says `follows the view` while a capture chose the
  size. The shape in the corner of the frame is always a standard ratio.
- The More settings summary sits in the frame's header, without field names.
- Runs are one line each with a moving indeterminate bar, and finished runs
  fold into one line with Clear finished. The explanation of Hide is its
  tooltip.
- The results strip is four square cells, including one for each run still in
  flight. Save carries the weight on the first row of actions; Reuse its
  inputs, Go to its view and Add to stage sit on a second row, only where they
  apply.
- **Choose a tool** opens with the search box focused. Kits are a row of filter
  pills with their counts, the two most recently picked tools are offered
  first, and every tile has a logo well.
- **Compare** names each input by what it is and where it came from rather than
  by an id, labels each side on the picture, has a grab handle on the seam
  that can be dragged or stepped with the arrow keys, and can Save, Send or Use
  in Create.
- The live viewfinder is drawn from a larger preview, so it stays sharp at the
  panel's full width.

## [0.2.0] - 2026-08-31

### Added

- **Load more** on the Library and Uploads tabs and in the image picker, adding
  the next page under what is on screen instead of replacing it. What has been
  paged in survives a tab switch.
- The library filters the web client offers over the same endpoint: tool, model
  family, media, aspect ratio, resolution, a date window, and favorites, drafted
  behind a collapsed Filters header and committed on Apply.
- `GET /api/v2/tool-tags`, which is what lets the model-family and media filters
  show labels rather than ids.
- **Refresh** beside Load more, since these grids are now held rather than
  refetched on every visit. Creating an upload from either path drops the held
  pages on its own; anything changed elsewhere needs the button.

- **Generate moved into the footer**, above BILLED TO, in the brand colour,
  with the cost line and the Results slider that go with it. It used to sit at
  the bottom of the Create tab's form, where a long form and a filling results
  strip pushed the one button that spends money off screen.
- **The status line is off by default**, with a switch for it on the Account
  tab. Failures are shown either way.

- **A video result shows its first frame.** The Generate API now returns a
  signed still beside a video output, which is fetched with the
  render and drawn on the tile and in the viewer, with the duration over it.
  A 3D asset, and a video whose still could not be made, keep the lettered
  tile they had.

- **View** on a selected input image, so a capture can be looked at at full
  size before it is sent rather than judged from a 58px thumbnail.
- **Capture from a named camera.** Every camera on the open stage is offered
  beside each image field, alongside the active viewport it always had. Picking
  one points the viewport at that camera, waits for the renderer to catch up,
  reads the frame, and puts your view back where it was. The camera is recorded
  with the capture, so a render made from the lobby camera still says so in the
  compare picker an hour later.
- **The library and your uploads can each be popped out into their own
  window**, so either one can be on screen beside the Create form instead of a
  tab away from it. Drag a window's title bar to dock it wherever suits. Both
  can be out at once, they are independent, closing a window puts that surface
  back in its tab, and whether you left each one popped out is remembered for
  next time.
- **Images in an image field wrap onto a second row** instead of running off
  the edge of the panel into a horizontal scroll. How many fit on a row follows
  the width the panel has been given.
- **Cameras are taken out of shot while a capture is taken.** Kit draws a small
  camera model for every camera on the stage, and it was landing in the picture
  sent to be generated from. It is hidden for the moment of the capture and put
  back straight after, with no change to your stage.

### Fixed

- Deleting the camera an image field was set to left the field remembering it
  with no way to change back, because the source dropdown is replaced by a
  label when only the active viewport is left. The field now returns to the
  active viewport on its own and says that it did.
- A capture that is refused says so in red under the Capture button, instead of
  only in the status line at the very bottom of the panel. The status line still
  carries it too.
- Capturing from a camera that had been deleted from the stage quietly
  captured the active viewport instead and recorded it as having come from the
  deleted camera. It now says the camera is gone and captures nothing.
- The library treated everything it listed as an image. It now carries the
  `type` and `mime_type` the listing publishes, so a video is downloaded under
  its own extension, opened in the system viewer rather than as an empty frame,
  named in the selected line, and not offered to an image field or to the
  picker. Save all and the save dialog stopped assuming PNG with it.

- Dropdown values (model, multi-select, output size) survived neither a tab
  switch nor Reuse its inputs: those three controls registered no restore, so
  they silently reverted to the tool's default.
- The image limit is counted per request rather than per field, and an image
  refused at the limit now says so instead of being dropped with a log line.
- A library video is no longer downloaded in full just to fail to draw a
  thumbnail from it, on every redraw.

- Every rebuild reached from a click now waits a frame. omni.ui refuses a
  container clear during event dispatch, and eight renderers were doing it:
  the library filters, the image rows, the results strip, the job list, the
  session section, the asset actions, the tool picker and the sign-in steps.

- A request that timed out on the READ escaped as a raw `TimeoutError` rather
  than an `ApiError`, killing the task loading the page and leaving the grid on
  "Loading..." for good. Transport failures are converted at one place now, and
  a background task that dies says so under this plugin's name instead of only
  as asyncio's "Task exception was never retrieved".

- A capture waited for the renderer without a time limit. A viewport that
  stopped delivering frames, which is what minimising Kit or pausing the
  viewport does, left the panel unable to capture again and left the camera
  models hidden on your stage for the rest of the session. The wait now gives
  up after 20 seconds, captures what is there, and puts everything back.
- Capturing says that it is capturing, under the button you pressed, for the
  active viewport as well as for a named camera. Waiting for the renderer to
  settle takes a moment on a heavy scene, and the panel used to sit there
  saying nothing.
- The camera an image field was set to was forgotten whenever the Create tab
  was rebuilt: visiting the Library and coming back, using an image from a
  grid, or reusing a past run's inputs. The field went back to the active
  viewport without saying so, and the next capture came from wherever you were
  orbiting. It is now carried across, the same way your typed values and chosen
  images already were.
- Signing out left a popped-out library window open, still listing the account
  you had just left and still able to download from it. Changing account left
  the same window showing the previous account's images while the footer named
  the new one. Signing out closes the window (and reopens it next time you sign
  in, if that is where you had it), and changing account rebuilds it.
- Closing the popped-out library window with its own X took you to the Library
  tab, even if you were part way through writing a prompt. It now leaves you
  where you were. Bring it back into this tab still takes you there, which is
  what it is for.
- The preview beside the selected image in a popped-out library went blank, and
  stayed blank, if you switched tabs in the main panel while it was loading.
- **View** on an image already chosen for a field offered Use in Create, which
  added the same picture to that field a second time and sent it twice.
- **A camera created with the panel open did not appear in the capture-source
  dropdown** until you switched tabs and switched back, or happened to add or
  remove an image. The list was only ever rebuilt with the image row, and
  authoring a camera does not rebuild it. The panel now watches the stage, so a
  camera appears as you make it and a deleted one drops out (and any field
  pointed at it says so) without waiting for anything else to happen.
- Clicking around an image field could stutter on a large scene. The list of
  cameras is now read when the stage actually changes rather than on every
  redraw of the row.

### Changed

- Back/More paging replaced by the accumulating Load more above.
- Grid thumbnails go through the same on-disk cache and rate limit as every
  other image the panel fetches, so redrawing a grid that has been paged deep
  costs nothing.
- **Use in Create** now opens the Create tab on the field it filled, instead of
  leaving you on the Library tab reading a status line that named the tab you
  then had to click. Choosing through the field's own picker already ended
  there, so the two routes now agree.
- Image viewer actions that take you somewhere else (Compare, Send to
  RunDiffusion, Open in system viewer, Add to stage, Use in Create) close the
  viewer as they go, rather than leaving it over the place they took you.
  Save to computer still leaves it open.
- Library and Uploads cells show **Use**, **Save** and **Open** across the
  bottom of the picture while the pointer is on them, so an item can be acted
  on where it is instead of being selected first. The row of actions under the
  grid is unchanged and still works.

## [0.1.0] - 2026-08-17

### Added

- First scaffold: a Kit extension registering an `omni.ui` panel with the
  prompt, capture, status, and gallery flow.
- In-memory active-viewport capture encoded to PNG with the prebundled Pillow.
- Host identity and v2 API constants.
