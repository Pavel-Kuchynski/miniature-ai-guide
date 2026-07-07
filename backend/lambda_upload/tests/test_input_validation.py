"""Tests for input validation in the presigned URL handler.

Tests request parsing, file name sanitization, and parameter handling
(query string vs JSON body, singular vs plural forms).
"""

import json

import pytest

from event_parser import (
    extract_job_id,
    normalize_to_list,
    parse_event,
    parse_expires_in,
    sanitize_file_name,
)


class TestNormalizeToList:
    """Tests for normalize_to_list helper."""

    def test_none_returns_empty_list(self) -> None:
        """None input returns empty list."""
        result = normalize_to_list(None)
        assert result == []

    def test_list_returns_list_of_strings(self) -> None:
        """List input is converted to strings."""
        result = normalize_to_list(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_list_filters_falsy_values(self) -> None:
        """List input filters out None and empty strings."""
        result = normalize_to_list(["a", None, "", "b"])
        assert result == ["a", "b"]

    def test_single_string_returns_list_with_one_item(self) -> None:
        """Single string returns a one-item list."""
        result = normalize_to_list("single.png")
        assert result == ["single.png"]

    def test_comma_separated_string_is_split(self) -> None:
        """Comma-separated string is parsed into a list."""
        result = normalize_to_list("file1.png, file2.png, file3.png")
        assert result == ["file1.png", "file2.png", "file3.png"]

    def test_comma_separated_with_whitespace_is_trimmed(self) -> None:
        """Whitespace around comma-separated values is stripped."""
        result = normalize_to_list("  file1.png  ,  file2.png  ")
        assert result == ["file1.png", "file2.png"]

    def test_numeric_value_is_converted_to_string(self) -> None:
        """Non-string scalar values are converted to strings."""
        result = normalize_to_list(42)
        assert result == ["42"]


class TestSanitizeFileName:
    """Tests for sanitize_file_name helper."""

    def test_safe_filename_unchanged(self) -> None:
        """Filename with only safe characters passes through."""
        result = sanitize_file_name("my-file.png", 0)
        assert result == "my-file.png"

    def test_unsafe_characters_replaced_with_underscore(self) -> None:
        """Unsafe characters are replaced with underscore."""
        result = sanitize_file_name("my file@#$.png", 0)
        assert result == "my_file___.png"

    def test_path_separators_stripped(self) -> None:
        """Path separators are stripped to base name only."""
        result = sanitize_file_name("../../../etc/passwd", 0)
        assert result == "passwd"

    def test_empty_base_name_returns_fallback(self) -> None:
        """Empty base name falls back to file_<index+1>.bin."""
        result = sanitize_file_name("", 0)
        assert result == "file_1.bin"

    def test_dot_only_filename_returns_fallback(self) -> None:
        """Filename of only '.' falls back to file_<index+1>.bin."""
        result = sanitize_file_name(".", 2)
        assert result == "file_3.bin"

    def test_double_dot_filename_returns_fallback(self) -> None:
        """Filename of only '..' falls back to file_<index+1>.bin."""
        result = sanitize_file_name("..", 1)
        assert result == "file_2.bin"

    def test_sanitized_to_empty_returns_fallback(self) -> None:
        """If sanitization results in empty string, use fallback."""
        result = sanitize_file_name("@#$%", 3)
        assert result == "file_4.bin"

    def test_filename_capped_at_255_characters(self) -> None:
        """Filename is truncated to 255 characters."""
        long_name = "a" * 300 + ".png"
        result = sanitize_file_name(long_name, 0)
        assert len(result) == 255


class TestParseExpiresIn:
    """Tests for parse_expires_in helper."""

    def test_valid_integer_string_returns_integer(self) -> None:
        """Valid integer string returns the integer value."""
        result = parse_expires_in("600")
        assert result == 600

    def test_zero_returns_none(self) -> None:
        """Zero is invalid and returns None."""
        result = parse_expires_in("0")
        assert result is None

    def test_negative_returns_none(self) -> None:
        """Negative values are invalid and return None."""
        result = parse_expires_in("-100")
        assert result is None

    def test_exceeds_max_returns_none(self) -> None:
        """Values exceeding 7 days (604800 seconds) return None."""
        result = parse_expires_in("999999")
        assert result is None

    def test_max_valid_value_is_accepted(self) -> None:
        """Maximum valid value (604800) is accepted."""
        result = parse_expires_in("604800")
        assert result == 604800

    def test_non_integer_string_returns_none(self) -> None:
        """Non-integer strings return None."""
        result = parse_expires_in("not-a-number")
        assert result is None


class TestExtractJobId:
    """Tests for extract_job_id helper."""

    def test_extracts_from_query_string(self) -> None:
        """jobId is extracted from query string parameters."""
        event = {"queryStringParameters": {"jobId": "test-job-123"}}
        result = extract_job_id(event)
        assert result == "test-job-123"

    def test_extracts_from_json_body(self) -> None:
        """jobId is extracted from JSON body."""
        event = {"body": json.dumps({"jobId": "body-job-456"})}
        result = extract_job_id(event)
        assert result == "body-job-456"

    def test_body_takes_precedence_over_query(self) -> None:
        """Body jobId takes precedence over query string jobId."""
        event = {
            "queryStringParameters": {"jobId": "query-job"},
            "body": json.dumps({"jobId": "body-job"}),
        }
        result = extract_job_id(event)
        assert result == "body-job"

    def test_missing_job_id_returns_unknown(self) -> None:
        """Missing jobId returns 'unknown'."""
        event = {"queryStringParameters": {}}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_malformed_json_returns_unknown(self) -> None:
        """Malformed JSON body returns 'unknown'."""
        event = {"body": "{not valid json}"}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_base64_encoded_body_is_decoded(self) -> None:
        """Base64-encoded body is decoded before parsing."""
        import base64

        body_json = json.dumps({"jobId": "decoded-job"})
        encoded = base64.b64encode(body_json.encode()).decode()
        event = {
            "body": encoded,
            "isBase64Encoded": True,
        }
        result = extract_job_id(event)
        assert result == "decoded-job"

    def test_invalid_base64_returns_unknown(self) -> None:
        """Invalid base64 encoding returns 'unknown'."""
        event = {
            "body": "not valid base64!!!",
            "isBase64Encoded": True,
        }
        result = extract_job_id(event)
        assert result == "unknown"


class TestParseEvent:
    """Tests for parse_event helper."""

    def test_parses_file_names_from_query_string(self) -> None:
        """File names are parsed from query string parameters."""
        event = {
            "queryStringParameters": {
                "fileNames": "file1.png,file2.png",
            }
        }
        names, types = parse_event(event)
        assert names == ["file1.png", "file2.png"]

    def test_parses_singular_file_name_from_query_string(self) -> None:
        """Singular 'fileName' is parsed from query string."""
        event = {
            "queryStringParameters": {
                "fileName": "single.png",
            }
        }
        names, types = parse_event(event)
        assert names == ["single.png"]

    def test_parses_content_types_from_query_string(self) -> None:
        """Content types are parsed from query string parameters."""
        event = {
            "queryStringParameters": {
                "contentTypes": "image/png,image/jpeg",
            }
        }
        names, types = parse_event(event)
        assert types == ["image/png", "image/jpeg"]

    def test_parses_file_names_from_json_body(self) -> None:
        """File names are parsed from JSON body."""
        event = {
            "body": json.dumps({
                "fileNames": ["body1.png", "body2.png"],
            }),
        }
        names, types = parse_event(event)
        assert names == ["body1.png", "body2.png"]

    def test_body_file_names_override_query_file_names(self) -> None:
        """Body file names take precedence over query string file names."""
        event = {
            "queryStringParameters": {"fileNames": "query.png"},
            "body": json.dumps({"fileNames": ["body.png"]}),
        }
        names, types = parse_event(event)
        assert names == ["body.png"]

    def test_body_content_types_override_query_content_types(self) -> None:
        """Body content types take precedence over query string content types."""
        event = {
            "queryStringParameters": {"contentTypes": "image/png"},
            "body": json.dumps({"contentTypes": ["image/jpeg"]}),
        }
        names, types = parse_event(event)
        assert types == ["image/jpeg"]

    def test_malformed_json_body_falls_back_to_query_string(self) -> None:
        """Malformed JSON body falls back to query string parsing."""
        event = {
            "queryStringParameters": {"fileName": "fallback.png"},
            "body": "{not valid json",
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]

    def test_base64_encoded_body_is_decoded(self) -> None:
        """Base64-encoded body is decoded before JSON parsing."""
        import base64

        body_json = json.dumps({"fileNames": ["decoded.png"]})
        encoded = base64.b64encode(body_json.encode()).decode()
        event = {
            "body": encoded,
            "isBase64Encoded": True,
        }
        names, types = parse_event(event)
        assert names == ["decoded.png"]

    def test_empty_event_returns_empty_lists(self) -> None:
        """Empty event returns empty lists."""
        names, types = parse_event({})
        assert names == []
        assert types == []

    def test_none_query_string_parameters_returns_empty_lists(self) -> None:
        """None queryStringParameters returns empty lists."""
        event = {"queryStringParameters": None}
        names, types = parse_event(event)
        assert names == []
        assert types == []
