"""What the compare window offers to wipe against.

The comparison stopped being "this render versus the live viewport" and became
"this render versus the image it was made from", which turns a fixed thing into
a list: a run can carry several inputs across several fields, and the picker has
to name each one well enough that a person recognises it a run later.

The window itself needs Kit. This is the part that does not: reading a run's
exported image state back into a labelled, ordered list. It is worth pinning
because the state is a tuple whose shape lives in `form.export_image_state`, and
a field added there in the wrong position would relabel every entry silently.
"""

from __future__ import annotations

from rundiffusion_omniverse.compare import compare_sources, result_source_name


def slot(
    slot_id, reference=None, description="", png=None, thumbnail=None, camera_path=None
):
    """One entry shaped exactly as `ToolForm.export_image_state` writes it."""
    return (slot_id, reference, description, png, thumbnail, camera_path)


def test_a_run_with_no_images_offers_nothing():
    assert compare_sources({}) == []
    assert compare_sources(None) == []


def test_a_capture_is_named_by_what_it_is_and_where_it_came_from():
    sources = compare_sources({"image": [slot(1, png=b"bytes")]})

    assert [s.label for s in sources] == ["Captured view · Active viewport"]
    assert sources[0].is_capture
    assert sources[0].side_label == "Captured view"
    assert sources[0].png == b"bytes"


def test_a_capture_from_a_camera_names_the_camera():
    sources = compare_sources(
        {"image": [slot(1, png=b"a", description="Captured from Lobby")]}
    )

    assert [s.label for s in sources] == ["Captured view · Lobby"]


def test_a_chosen_image_is_named_by_its_file():
    """The form row says "Sending kitchen.png." because that reads as a
    sentence in a row. In a dropdown the sentence is noise."""
    sources = compare_sources(
        {"image": [slot(4, reference="lib:1", description="Sending kitchen.png.")]}
    )

    assert [s.label for s in sources] == ["Chosen image · kitchen.png"]
    assert not sources[0].is_capture
    assert sources[0].side_label == "Chosen image"


def test_a_chosen_image_with_no_description_is_named_by_its_kind():
    sources = compare_sources({"image": [slot(4, reference="lib:1")]})

    assert [s.label for s in sources] == ["Chosen image"]


def test_a_generated_identifier_is_not_shown_as_a_name():
    """ "Compared against 91BDB9F7-A40B..." identified nothing to a person."""
    sources = compare_sources(
        {
            "image": [
                slot(
                    4,
                    reference="lib:1",
                    description="Sending 91BDB9F7-A40B-4C2E-9F11-0123456789AB.png.",
                )
            ]
        }
    )

    assert [s.label for s in sources] == ["Chosen image"]


def test_a_run_names_where_its_first_input_came_from():
    assert result_source_name({"image": [slot(1, png=b"a")]}) == "Active viewport"
    assert (
        result_source_name({"image": [slot(1, png=b"a", description="Captured from Lobby")]})
        == "Lobby"
    )
    assert result_source_name({"image": []}) == ""
    assert result_source_name(None) == ""


def test_several_images_in_one_field_carry_their_position():
    """Which is the whole reason a plural field exists: the images differ, and
    the same words three times would be three identical dropdown entries."""
    sources = compare_sources(
        {
            "images": [
                slot(1, png=b"a"),
                slot(2, png=b"b"),
                slot(3, reference="lib:9", description="Sending plan.png."),
            ]
        }
    )

    assert [s.label for s in sources] == [
        "IMAGES 1 · Captured view · Active viewport",
        "IMAGES 2 · Captured view · Active viewport",
        "IMAGES 3 · Chosen image · plan.png",
    ]


def test_the_order_is_the_order_the_run_was_sent_in():
    """Fields first, then position within a field, which is what the panel
    numbers the file parts by. A picker in a different order would have someone
    comparing against an image they did not think they were choosing."""
    sources = compare_sources(
        {
            "first": [slot(1, png=b"a"), slot(2, png=b"b")],
            "second": [slot(3, png=b"c")],
        }
    )

    assert [s.slot_id for s in sources] == [1, 2, 3]


def test_a_field_key_reads_as_words_where_the_field_is_named():
    """The field leads only where the run carried more than one image, which
    is the only time it tells two rows apart."""
    sources = compare_sources(
        {"reference_image": [slot(1, png=b"a")], "mask": [slot(2, png=b"b")]}
    )

    assert sources[0].label == "REFERENCE IMAGE · Captured view · Active viewport"


def test_an_empty_field_contributes_nothing():
    """Every image field is exported, including the ones nobody filled."""
    sources = compare_sources({"image": [], "mask": [slot(2, png=b"a")]})

    assert [s.label for s in sources] == ["Captured view · Active viewport"]


def test_the_slot_id_survives_so_a_library_image_can_be_found_again():
    """A chosen image is a reference, not bytes: the panel looks the asset up by
    SLOT id to get a URL it can download. Position would not do, because
    removing an earlier image renumbers everything after it."""
    sources = compare_sources(
        {"image": [slot(7, reference="lib:1"), slot(9, reference="lib:2")]}
    )

    assert [s.slot_id for s in sources] == [7, 9]
