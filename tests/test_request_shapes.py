"""Tests for the shapes this plugin puts on the wire.

Every bug this plugin has shipped so far has been a wrong shape rather than
wrong logic: a guessed field name, a guessed content type, a descriptor where a
list belonged. None of them failed loudly, and two were found only by a human
looking at the screen. These tests pin the shapes that have already broken once,
and they run without Kit, a GPU, or the network.
"""

from __future__ import annotations

from rundiffusion_omniverse.api.generate import CAPTURE_MIME_TYPE, capture_file_parts
from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.tools import (
    ImageSelection,
    ToolDetail,
    ToolField,
    build_inputs,
    missing_required_fields,
    ordered_captures,
)


def _capture(marker: bytes = b"PNG") -> ImageSelection:
    """One view, captured and pinned. The bytes travel with the request.

    Distinct bytes per capture in these tests on purpose: two captures used to
    be two copies of one frame, and the whole point of pinning them is that they
    are now genuinely different images.
    """
    return ImageSelection(png=marker)


def _upload(asset_id: str) -> ImageSelection:
    return ImageSelection(reference={"kind": "UPLOAD_REF", "id": asset_id})
from rundiffusion_omniverse.api.transport import _multipart_body


def _tool(*fields: ToolField) -> ToolDetail:
    return ToolDetail(id="t", name="T", tool_fields_hash="v1:abc", fields=list(fields))


class TestMultipartBody:
    def test_file_part_declares_its_real_content_type(self):
        """The server allow-lists the part's MIME and answers 415 otherwise.

        Shipped as application/octet-stream once, which production rejected with
        "has unsupported MIME".
        """
        body = _multipart_body(
            {"payload": "{}"},
            [("files", "omniverse-output.png", b"\x89PNG\r\n", CAPTURE_MIME_TYPE)],
            "BOUND",
        ).decode("utf-8", "replace")

        assert "Content-Type: image/png" in body
        assert "application/octet-stream" not in body

    def test_carries_the_payload_part_the_server_requires(self):
        """Multipart requests must include a `payload` JSON part, or 400."""
        body = _multipart_body({"payload": '{"tool_id": "x"}'}, [], "BOUND").decode()

        assert 'name="payload"' in body
        assert '{"tool_id": "x"}' in body


class TestBuildInputs:
    def test_inputs_are_keyed_by_field_key(self):
        """v2 keys `inputs` by fieldKey, not by slug, label, or uuid."""
        inputs = build_inputs(
            _tool(ToolField(key="prompt_key", type="PROMPT", label="Prompt", required=True)),
            {"prompt_key": "a castle"},
            has_capture=False,
        )

        assert inputs == {"prompt_key": "a castle"}

    def test_singular_image_field_takes_one_descriptor(self):
        inputs = build_inputs(
            _tool(ToolField(key="img", type="IMG", label=None, required=True)),
            {},
            image_selections={"img": [_capture()]},
        )

        assert inputs["img"] == {"kind": "MULTIPART", "file_index": 0}

    def test_plural_image_field_takes_a_list(self):
        """IMGS takes a LIST. A bare descriptor is accepted and then mishandled
        downstream rather than rejected, which is how this shipped as a bug."""
        inputs = build_inputs(
            _tool(ToolField(key="imgs", type="IMGS", label=None, required=True)),
            {},
            image_selections={"imgs": [_capture()]},
        )

        assert inputs["imgs"] == [{"kind": "MULTIPART", "file_index": 0}]

    def test_file_index_is_snake_case(self):
        """v1 spelled it fileIndex; v2 spells it file_index."""
        inputs = build_inputs(
            _tool(ToolField(key="img", type="IMG", label=None, required=True)),
            {},
            image_selections={"img": [_capture()]},
        )

        assert "file_index" in inputs["img"]
        assert "fileIndex" not in inputs["img"]

    def test_values_pass_through_under_their_own_keys(self):
        """The form collects by fieldKey, so build_inputs no longer guesses
        which field the prompt belongs in. It used to, and picking wrong meant
        writing the user's words into a negative prompt."""
        inputs = build_inputs(
            _tool(
                ToolField(key="pos", type="PROMPT", label="Prompt", required=True),
                ToolField(key="neg", type="NEG_PROMPT", label="Negative", required=False),
            ),
            {"pos": "a castle", "neg": "blurry"},
            has_capture=False,
        )

        assert inputs == {"pos": "a castle", "neg": "blurry"}

    def test_no_capture_means_no_image_descriptor(self):
        """preview-cost prices the run before anything is uploaded."""
        inputs = build_inputs(
            _tool(ToolField(key="img", type="IMG", label=None, required=True)),
            {},
            has_capture=False,
            image_selections={"img": [_capture()]},
        )

        assert inputs == {}


class TestPerFieldImageSelections:
    """Any image field can hold captured views, references, or nothing.

    Selections are per field and ordered, and POSITION IS THE CONTRACT: capture
    descriptors are numbered walking the schema, and the caller stages their
    bytes on the same walk.
    """

    def test_each_capture_gets_its_own_file_index(self):
        inputs = build_inputs(
            _tool(
                ToolField(key="start", type="IMG", label="Start", required=True),
                ToolField(key="end", type="IMG", label="End", required=True),
            ),
            {},
            image_selections={"start": [_capture(b"A")], "end": [_capture(b"B")]},
        )

        assert inputs["start"] == {"kind": "MULTIPART", "file_index": 0}
        assert inputs["end"] == {"kind": "MULTIPART", "file_index": 1}

    def test_indexes_follow_the_schema_order_not_the_selection_order(self):
        """Both sides walk the schema, so neither can invent an order the other
        does not share. Getting this wrong sends each field the OTHER field's
        image: a run that succeeds and returns the wrong picture."""
        inputs = build_inputs(
            _tool(
                ToolField(key="start", type="IMG", label="Start", required=True),
                ToolField(key="end", type="IMG", label="End", required=True),
            ),
            {},
            image_selections={"end": [_capture(b"B")], "start": [_capture(b"A")]},
        )

        assert inputs["start"] == {"kind": "MULTIPART", "file_index": 0}
        assert inputs["end"] == {"kind": "MULTIPART", "file_index": 1}

    def test_a_plural_field_carries_several_images_in_the_order_given(self):
        inputs = build_inputs(
            _tool(ToolField(key="imgs", type="IMGS", label=None, required=True)),
            {},
            image_selections={"imgs": [_upload("u1"), _capture(), _upload("u2")]},
        )

        assert inputs["imgs"] == [
            {"kind": "UPLOAD_REF", "id": "u1"},
            {"kind": "MULTIPART", "file_index": 0},
            {"kind": "UPLOAD_REF", "id": "u2"},
        ]

    def test_a_singular_field_takes_the_first_image_not_a_list(self):
        """IMG takes a bare value. A list where one belongs is the same class of
        mistake as a bare value where a list belongs."""
        inputs = build_inputs(
            _tool(ToolField(key="img", type="IMG", label=None, required=True)),
            {},
            image_selections={"img": [_upload("u1")]},
        )

        assert inputs["img"] == {"kind": "UPLOAD_REF", "id": "u1"}

    def test_a_field_left_empty_is_omitted_entirely(self):
        inputs = build_inputs(
            _tool(
                ToolField(key="start", type="IMG", label="Start", required=True),
                ToolField(key="end", type="IMG", label="End", required=False),
            ),
            {},
            image_selections={"start": [_capture()], "end": []},
        )

        assert "end" not in inputs

    def test_no_selections_at_all_means_no_images(self):
        """Nothing is filled in on the user's behalf. A capture is real bytes
        taken at a real moment, so there is nothing to assume."""
        inputs = build_inputs(
            _tool(ToolField(key="img", type="IMG", label=None, required=True)),
            {},
        )

        assert inputs == {}

    def test_the_preview_cost_path_drops_captures_and_keeps_references(self):
        """preview-cost prices the run before anything is uploaded, but a
        library image is already on the server and still prices."""
        inputs = build_inputs(
            _tool(ToolField(key="imgs", type="IMGS", label=None, required=True)),
            {},
            has_capture=False,
            image_selections={"imgs": [_capture(), _upload("u1")]},
        )

        assert inputs["imgs"] == [{"kind": "UPLOAD_REF", "id": "u1"}]

    def test_a_capture_with_no_bytes_is_not_numbered_into_a_gap(self):
        """There is no part for it, so a descriptor pointing at one would name a
        file that is not in the request."""
        inputs = build_inputs(
            _tool(ToolField(key="imgs", type="IMGS", label=None, required=True)),
            {},
            image_selections={
                "imgs": [ImageSelection(png=None), _capture(b"REAL")],
            },
        )

        assert inputs["imgs"] == [{"kind": "MULTIPART", "file_index": 0}]


class TestCapturePartsMatchTheirDescriptors:
    """The file parts and the file_index values are one contract in two places.

    `build_inputs` numbers the captures from zero walking the schema, and
    `ordered_captures` collects their bytes on the same walk. If they ever
    disagree, an image points at a part that is not there (the run dies with an
    empty error) or at the wrong one (the run succeeds and returns the wrong
    picture, which is worse).
    """

    def _two_field_tool(self):
        return _tool(
            ToolField(key="start", type="IMG", label=None, required=True),
            ToolField(key="imgs", type="IMGS", label=None, required=False),
        )

    def test_one_part_per_captured_image(self):
        parts = capture_file_parts([b"A", b"B", b"C"])

        assert len(parts) == 3
        assert all(part[3] == CAPTURE_MIME_TYPE for part in parts)

    def test_each_part_carries_its_own_bytes(self):
        """Two captures are two different views now, so sending one twice would
        quietly send the wrong picture for the second."""
        parts = capture_file_parts([b"FIRST", b"SECOND"])

        assert [part[2] for part in parts] == [b"FIRST", b"SECOND"]

    def test_no_captures_means_no_parts_at_all(self):
        """Every image came from the library, so no viewport was ever read."""
        assert capture_file_parts([]) == []

    def test_references_contribute_no_parts(self):
        selections = {"imgs": [_upload("u1"), _capture(b"A")], "start": [_upload("u2")]}

        assert ordered_captures(self._two_field_tool(), selections) == [b"A"]

    def test_the_nth_part_is_the_image_the_nth_descriptor_means(self):
        """The invariant, checked across both functions rather than in each."""
        selections = {
            "start": [_capture(b"FROM-START")],
            "imgs": [_upload("u1"), _capture(b"FROM-IMGS-1"), _capture(b"FROM-IMGS-2")],
        }
        tool = self._two_field_tool()
        inputs = build_inputs(tool, {}, image_selections=selections)
        parts = capture_file_parts(ordered_captures(tool, selections))

        # The descriptors, paired with the bytes they name.
        named = {}
        for key, value in inputs.items():
            for descriptor in value if isinstance(value, list) else [value]:
                if descriptor.get("kind") == "MULTIPART":
                    named[key] = parts[descriptor["file_index"]][2]

        assert named["start"] == b"FROM-START"
        assert named["imgs"] == b"FROM-IMGS-2"  # the last descriptor written for it
        assert [part[2] for part in parts] == [
            b"FROM-START",
            b"FROM-IMGS-1",
            b"FROM-IMGS-2",
        ]


class TestUnsupportedFields:
    def test_a_required_model_field_is_reported(self):
        """Refused before submitting, rather than billing for a run that cannot
        succeed."""
        tool = _tool(ToolField(key="m", type="MODEL", label=None, required=True))

        assert tool.unsupported_required_field is not None

    def test_an_optional_unsupported_field_is_not_blocking(self):
        tool = _tool(ToolField(key="v", type="VIDEO", label=None, required=False))

        assert tool.unsupported_required_field is None


class TestIdentity:
    def test_display_name_prefers_email(self):
        session = RdSession()
        session.user = {"email": "a@b.com", "uid": "u1"}

        assert session.display_name == "a@b.com"

    def test_display_name_is_none_when_unknown(self):
        """It must NOT fall back to a literal. Doing so rendered the account
        line as "Signed in as Signed in" after a restart."""
        assert RdSession().display_name is None

    def test_account_kind_drives_is_personal(self):
        """The API sends `label` and `kind`, not `name` and `is_personal`."""
        assert Account(id=None, label="Personal", kind="PERSONAL").is_personal
        assert not Account(id="t1", label="Acme", kind="TEAM").is_personal


class TestRequiredFieldDefaults:
    """The failure this class of test exists for: a run the server ACCEPTS and
    then fails with an empty error, because a required field it needed was
    never sent. Juggernaut Lightning Flux also requires WIDTH_HEIGHT, STEPS and
    SEED, none of which this panel has a control for."""

    def test_required_fields_fall_back_to_their_default(self):
        inputs = build_inputs(
            _tool(
                ToolField(key="prompt", type="PROMPT", label=None, required=True),
                ToolField(key="steps", type="STEPS", label=None, required=True, default_value=4),
                ToolField(
                    key="wh",
                    type="WIDTH_HEIGHT",
                    label=None,
                    required=True,
                    default_value={"width": 1024, "height": 1024},
                ),
            ),
            {"prompt": "a castle"},
            has_capture=False,
        )

        assert inputs["steps"] == 4
        # Passed through as the tool published it. The sub-keys of a composite
        # are the tool's own private mappings, so inventing them would be
        # guessing a shape.
        assert inputs["wh"] == {"width": 1024, "height": 1024}

    def test_an_explicit_value_beats_the_default(self):
        inputs = build_inputs(
            _tool(
                ToolField(
                    key="prompt", type="PROMPT", label=None, required=True,
                    default_value="unused default",
                ),
            ),
            {"prompt": "a castle"},
            has_capture=False,
        )

        assert inputs["prompt"] == "a castle"

    def test_optional_fields_are_left_alone(self):
        """Sending every default would override tool behaviour nobody asked to
        change. Only REQUIRED fields are filled."""
        inputs = build_inputs(
            _tool(
                ToolField(key="prompt", type="PROMPT", label=None, required=True),
                ToolField(key="cfg", type="CFG", label=None, required=False, default_value=7),
            ),
            {"prompt": "a castle"},
            has_capture=False,
        )

        assert "cfg" not in inputs

    def test_a_required_field_with_no_default_is_reported(self):
        tool = _tool(
            ToolField(key="prompt", type="PROMPT", label=None, required=True),
            ToolField(key="mystery", type="SOMETHING_NEW", label="Mystery", required=True),
        )
        inputs = build_inputs(tool, {"prompt": "a castle"}, has_capture=False)

        missing = missing_required_fields(tool, inputs)

        assert [f.key for f in missing] == ["mystery"]

    def test_nothing_is_missing_once_defaults_are_applied(self):
        tool = _tool(
            ToolField(key="prompt", type="PROMPT", label=None, required=True),
            ToolField(key="seed", type="SEED", label=None, required=True, default_value=0),
        )
        inputs = build_inputs(tool, {"prompt": "a castle"}, has_capture=False)

        assert missing_required_fields(tool, inputs) == []
