"""A requirement's brief quote may differ only in spacing; the stored quote is the brief's text."""

from agent_dev.domain.studio import locate_quote

BRIEF = "## 회의실 예약\n팀원은 회의실을 30분 단위로\n예약할 수 있어야 한다.\n관리자는 예약을 취소할 수 있다."


def test_exact_quote_is_kept():
    assert locate_quote("관리자는 예약을 취소할 수 있다.", BRIEF) == "관리자는 예약을 취소할 수 있다."


def test_rewrapped_line_returns_the_brief_wording():
    assert locate_quote("팀원은 회의실을 30분 단위로 예약할 수 있어야 한다.", BRIEF) == (
        "팀원은 회의실을 30분 단위로\n예약할 수 있어야 한다."
    )


def test_korean_spacing_differences_are_tolerated():
    assert locate_quote("회의실예약", BRIEF) == "회의실 예약"
    assert locate_quote("관리자는  예약을\t취소할 수있다.", BRIEF) == "관리자는 예약을 취소할 수 있다."


def test_changed_words_or_punctuation_still_fail():
    assert locate_quote("관리자는 예약을 삭제할 수 있다.", BRIEF) is None
    assert locate_quote("관리자는 예약을 취소할 수 있다!", BRIEF) is None
    assert locate_quote("   ", BRIEF) is None
