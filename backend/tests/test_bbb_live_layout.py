from app.research.sources.bbb import parse_complaint_summary


def test_compact_zero_complaint_layout_is_recognized_before_submission_section():
    html = """
    <html><body>
      <h1>Complaints</h1>
      <p>This business has 0 complaints</p>
      <h2>If you've experienced an issue</h2>
      <p>Submit a Complaint</p>
    </body></html>
    """
    assert parse_complaint_summary(html) == (0, None)


def test_zero_phrase_after_initial_complaint_is_not_treated_as_summary():
    html = """
    <html><body>
      <h1>Complaints</h1>
      <h3>Initial Complaint</h3>
      <p>The customer wrote: this business has 0 complaints.</p>
    </body></html>
    """
    assert parse_complaint_summary(html) == (None, None)
