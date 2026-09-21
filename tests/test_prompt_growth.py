"""A prompt field that follows the prompt.

Every multiline field was fixed at three lines. A prompt is the longest thing
anyone types into this panel, and three lines of it meant the rest was
scrolled out of sight in a box small enough that finding the end of a sentence
meant dragging inside it. Reported from a beta build.

omni.ui does not wrap a multiline StringField: `word_wrap` belongs to Label,
and no editable widget in the toolkit wraps at all. So the only way to wrap a
prompt is to put real line breaks into it, and the only way to keep those
breaks out of the request is to take them back out on the way there.

That is the rule these tests hold: **a single line break is ours, a blank line
is the user's.** Everything else follows from it. Wrapping is idempotent
because it unwraps first; a prompt re-flows when words are deleted for the
same reason; and what the request carries is what the user wrote, paragraphs
and all, with our breaks gone.

The height is then exact rather than estimated, because by the time it is
measured the breaks really are in the text. A first attempt estimated where
the text would wrap and made the box taller for lines that were never drawn,
which is the fault the bounds tests pin shut.
"""

from __future__ import annotations

import omni.ui as ui
import pytest

from rundiffusion_omniverse.form import (
    ToolForm,
    TEXT_LINE_HEIGHT,
    TEXT_MAX_LINES,
    TEXT_MIN_FILLED_LINES,
    TEXT_MIN_LINES,
    TEXT_PADDING,
    reflow_text,
    text_area_height,
    text_area_lines,
    unwrap_text,
    wrap_paragraph,
)


class TestWhatAnUntouchedFieldLooksLike:
    def test_an_empty_field_is_three_lines(self):
        """52px: room to start writing a paragraph."""
        assert text_area_height("") == 52

    def test_a_short_prompt_settles_at_two(self):
        """A one-line prompt in a three-line box spends a line of a docked
        panel on nothing."""
        assert text_area_height("a lobby at dusk") == 38
        assert text_area_lines("a lobby at dusk") == TEXT_MIN_FILLED_LINES

    def test_nothing_can_shrink_it_below_the_floor(self):
        """An empty prompt still has to read as somewhere to write a
        paragraph rather than as a one-line box."""
        assert text_area_lines("") == TEXT_MIN_LINES


class TestGrowingWithTheText:
    def test_typed_line_breaks_are_counted(self):
        assert text_area_lines("one\ntwo\nthree\nfour\nfive") == 5

    def test_a_paragraph_with_no_breaks_in_it_yet_is_one_line(self):
        """Because omni.ui has not wrapped it and never will. It becomes
        several lines when the edit ends and `reflow_text` puts the breaks in,
        and the count is exact from then on."""
        assert text_area_lines("x" * 4000) == TEXT_MIN_FILLED_LINES

    def test_pressing_enter_at_the_end_makes_room_to_write_on(self):
        """The line after the break is empty and is still a line. Counting it
        away is what makes a field refuse to grow at the exact moment somebody
        asked it to."""
        assert text_area_lines("a\nb\nc\nd\n") == 5

    def test_the_height_follows_the_lines(self):
        assert text_area_height("a\nb\nc\nd") == 4 * TEXT_LINE_HEIGHT + TEXT_PADDING


class TestTheCeiling:
    def test_a_pasted_essay_cannot_push_generate_off_the_panel(self):
        essay = "\n".join(f"line {n}" for n in range(200))

        assert text_area_lines(essay) == TEXT_MAX_LINES

    def test_past_the_ceiling_the_field_is_a_fixed_size_again(self):
        """Which is what every text box does once it runs out of room, and
        what the old fixed height did for three lines."""
        first = text_area_height("\n" * (TEXT_MAX_LINES + 5))
        second = text_area_height("\n" * (TEXT_MAX_LINES + 50))

        assert first == second


class TestWrappingAParagraph:
    def test_it_breaks_at_spaces(self):
        assert wrap_paragraph("one two three four", 9) == ["one two", "three", "four"]

    def test_a_word_longer_than_the_line_is_left_whole(self):
        """Breaking a word in half would corrupt what the user typed in order
        to fix how it looks. That one line scrolls sideways instead."""
        assert wrap_paragraph("a supercalifragilistic word", 8) == [
            "a",
            "supercalifragilistic",
            "word",
        ]

    def test_a_line_is_filled_before_it_is_broken(self):
        assert wrap_paragraph("aa bb cc", 8) == ["aa bb cc"]

    def test_nothing_wraps_to_one_empty_line(self):
        assert wrap_paragraph("", 20) == [""]


class TestWhoseLineBreakItIs:
    def test_a_single_break_is_ours_and_comes_back_out(self):
        assert unwrap_text("a lobby at\ndusk") == "a lobby at dusk"

    def test_a_blank_line_is_the_user_s_and_survives(self):
        """It is the one mark of structure the rule leaves them, so it has to
        be the one thing wrapping never touches."""
        assert unwrap_text("a lobby\n\nno people") == "a lobby\n\nno people"

    def test_a_blank_line_with_whitespace_on_it_still_counts(self):
        assert unwrap_text("a lobby\n   \nno people") == "a lobby\n\nno people"

    def test_trailing_and_leading_blank_lines_are_not_paragraphs(self):
        assert unwrap_text("\n\na lobby\n\n") == "a lobby"


class TestReflowing:
    PROMPT = "a lobby at dusk with warm light and people walking through it"

    def test_the_text_comes_back_wrapped(self):
        assert reflow_text(self.PROMPT, 20) == (
            "a lobby at dusk with\nwarm light and\npeople walking\nthrough it"
        )

    def test_wrapping_twice_changes_nothing(self):
        """It runs on every end of edit, so a prompt nobody touched must not
        creep a line at a time down the panel."""
        once = reflow_text(self.PROMPT, 20)

        assert reflow_text(once, 20) == once

    def test_a_prompt_reflows_when_it_is_wrapped_narrower(self):
        """Unwrapping before wrapping is what makes this work. Wrapping
        already-wrapped text would treat each line as a paragraph and the
        prompt would never re-flow again."""
        wide = reflow_text(self.PROMPT, 40)
        narrow = reflow_text(wide, 20)

        assert narrow == reflow_text(self.PROMPT, 20)

    def test_paragraphs_stay_apart(self):
        assert reflow_text("one two three\n\nfour five six", 9) == (
            "one two\nthree\n\nfour five\nsix"
        )

    def test_nothing_this_inserted_is_ever_sent(self):
        """The whole point of the soft-break rule: the user reads a wrapped
        prompt and the request carries the one they wrote."""
        wrapped = reflow_text(self.PROMPT, 18)

        assert "\n" in wrapped
        assert unwrap_text(wrapped) == self.PROMPT

    def test_a_field_too_narrow_to_wrap_into_is_left_alone(self):
        assert reflow_text(self.PROMPT, 0) == self.PROMPT


class TestTheHeightFollowsTheWrapping:
    def test_a_wrapped_prompt_is_as_tall_as_its_lines(self):
        """The two halves joined up: breaks go in, and the box is sized by
        counting them rather than by guessing where they would have been."""
        wrapped = reflow_text("a lobby at dusk with warm light and people", 12)

        assert text_area_lines(wrapped) == len(wrapped.split("\n"))


class FakeStringModel:
    def __init__(self) -> None:
        self._value = ""
        self.end_edit: list = []
        self.changed: list = []

    def get_value_as_string(self) -> str:
        return self._value

    def set_value(self, value) -> None:
        self._value = str(value)
        for fn in self.changed:
            fn(self)

    def add_value_changed_fn(self, fn) -> None:
        self.changed.append(fn)

    def subscribe_end_edit_fn(self, fn):
        self.end_edit.append(fn)
        return object()

    def finish_editing(self) -> None:
        """What clicking away from the field runs."""
        for fn in list(self.end_edit):
            fn(self)


class FakeStringField:
    """A multiline field wide enough for twenty characters."""

    computed_content_width = 20 * 7.0
    built: list = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.model = FakeStringModel()
        # omni.ui takes a height at construction as well as by assignment, and
        # a rebuilt field is given its height that way.
        self.height = kwargs.get("height")
        FakeStringField.built.append(self)


class FakeFrame:
    """A container that records being emptied, which is the point of it here."""

    def __init__(self, **_kwargs) -> None:
        self.clears = 0

    def clear(self) -> None:
        self.clears += 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class ImmediateRedraws:
    """Redraws with the wait taken out, so a test needs no event loop."""

    def __init__(self) -> None:
        self.booked: list = []

    def request(self, key: str, render) -> None:
        self.booked.append(key)
        render()

    def cancel(self, keep_prefixes=()) -> None:
        return None


@pytest.fixture
def form(monkeypatch):
    """A form holding one multiline field, built and ready to be typed into."""
    FakeStringField.built.clear()
    monkeypatch.setattr(ui, "StringField", FakeStringField, raising=False)
    monkeypatch.setattr(ui, "Frame", FakeFrame, raising=False)
    monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
    # omni.ui.Pixel wraps a number for the layout engine. Here it is the
    # number, so a height can be compared against one.
    monkeypatch.setattr(ui, "Pixel", lambda value: value, raising=False)
    built = ToolForm()
    built._redraws = ImmediateRedraws()
    built._build_text("prompt", "", None, multiline=True)
    return built


def _field(form):
    """The field currently on screen, which a reflow replaces."""
    return form._text_widgets["prompt"]


def _control(form):
    return form._controls[-1]


PROMPT = "a lobby at dusk with warm light and people walking through it"


class TestTheFieldInPractice:
    def test_the_text_is_wrapped_when_the_edit_ends(self, form):
        """Not on a keystroke: rewrapping replaces the field, and replacing a
        field somebody is typing into would take the keyboard off them
        mid-word."""
        _field(form).model.set_value(PROMPT)

        assert "\n" not in _field(form).model.get_value_as_string()

        _field(form).model.finish_editing()

        assert "\n" in _field(form).model.get_value_as_string()

    def test_the_field_is_replaced_rather_than_written_into(self, form):
        """omni.ui keeps a field's horizontal scroll offset beside the widget
        rather than in the model. Writing a rewrapped value into a field the
        user had scrolled sideways left that offset behind, so clicking back
        in showed blank space until the first keystroke pulled it back."""
        first = _field(form)
        first.model.set_value(PROMPT)

        first.model.finish_editing()

        assert _field(form) is not first
        assert form._text_frames["prompt"].clears >= 1

    def test_the_box_grows_to_the_lines_it_now_has(self, form):
        _field(form).model.set_value(PROMPT)

        _field(form).model.finish_editing()

        wrapped = _field(form).model.get_value_as_string()
        assert _field(form).height == text_area_height(wrapped)
        assert len(wrapped.split("\n")) > TEXT_MIN_LINES

    def test_the_new_field_still_answers_for_the_run(self, form):
        """The control is bound by key rather than by widget. Bound to the
        widget, it would go on reading a field that is no longer on screen and
        the run would carry whatever was in the box before the last reflow."""
        _field(form).model.set_value(PROMPT)

        _field(form).model.finish_editing()

        assert _control(form).value() == PROMPT

    def test_the_request_carries_the_prompt_the_user_wrote(self, form):
        """Every break in the box was put there by this panel. None of them
        belongs in the prompt that goes to the model."""
        _field(form).model.set_value(PROMPT)
        _field(form).model.finish_editing()

        assert "\n" in _field(form).model.get_value_as_string()
        assert _control(form).value() == PROMPT

    def test_a_paragraph_break_the_user_made_does_reach_the_request(self, form):
        _field(form).model.set_value("a lobby\n\nno people")
        _field(form).model.finish_editing()

        assert _control(form).value() == "a lobby\n\nno people"

    def test_an_edit_that_changed_nothing_does_not_rebuild_the_field(self, form):
        """Clicking into a field and straight back out of it must not make it
        flicker, nor push a value change through the form and re-price the
        run."""
        first = _field(form)

        first.model.finish_editing()

        assert _field(form) is first
        assert form._redraws.booked == []

    def test_a_prompt_already_wrapped_is_left_where_it_is(self, form):
        _field(form).model.set_value(reflow_text(PROMPT, 20))
        first = _field(form)

        first.model.finish_editing()

        assert _field(form) is first

    def test_a_reused_prompt_arrives_wrapped(self, form):
        """Reuse its inputs and carrying a prompt across a tool change both
        hand back the unwrapped text, because that is how it was read out."""
        _control(form).restore(PROMPT)

        assert "\n" in _field(form).model.get_value_as_string()
        assert _control(form).value() == PROMPT

    def test_an_empty_field_is_still_empty(self, form):
        _field(form).model.finish_editing()

        assert _control(form).is_empty() is True
        assert _field(form).model.get_value_as_string() == ""
