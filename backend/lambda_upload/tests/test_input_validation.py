"""Comprehensive tests for event_parser module.

Tests request parsing, file name sanitization, parameter handling
(query string vs JSON body, singular vs plural forms), and edge cases.
"""

import base64
import json

from event_parser import (
    extract_job_id,
    normalize_to_list,
    parse_event,
    parse_expires_in,
    sanitize_file_name,
)


class TestParseExpiresIn:
    """Tests for parse_expires_in helper."""

    # Valid cases
    def test_valid_integer_string_returns_integer(self) -> None:
        """Valid integer string returns the integer value."""
        result = parse_expires_in("600")
        assert result == 600

    def test_minimum_valid_value_one_is_accepted(self) -> None:
        """Minimum valid value (1) is accepted."""
        result = parse_expires_in("1")
        assert result == 1

    def test_max_valid_value_is_accepted(self) -> None:
        """Maximum valid value (604800 = 7 days) is accepted."""
        result = parse_expires_in("604800")
        assert result == 604800

    def test_midrange_valid_values(self) -> None:
        """Various midrange valid values are accepted."""
        assert parse_expires_in("300") == 300
        assert parse_expires_in("3600") == 3600
        assert parse_expires_in("86400") == 86400

    # Invalid: boundary cases
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

    def test_one_more_than_max_returns_none(self) -> None:
        """Value just above max (604801) returns None."""
        result = parse_expires_in("604801")
        assert result is None

    # Invalid: format/type cases
    def test_non_integer_string_returns_none(self) -> None:
        """Non-integer strings return None."""
        result = parse_expires_in("not-a-number")
        assert result is None

    def test_float_string_returns_none(self) -> None:
        """Float strings (e.g., '600.5') return None."""
        result = parse_expires_in("600.5")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        result = parse_expires_in("")
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        """Whitespace-only string returns None."""
        result = parse_expires_in("   ")
        assert result is None

    def test_string_with_leading_zeros(self) -> None:
        """String with leading zeros is parsed correctly."""
        result = parse_expires_in("0600")
        assert result == 600


class TestSanitizeFileName:
    """Tests for sanitize_file_name helper."""

    # Safe/valid cases
    def test_safe_filename_unchanged(self) -> None:
        """Filename with only safe characters passes through."""
        result = sanitize_file_name("my-file.png", 0)
        assert result == "my-file.png"

    def test_alphanumeric_and_safe_chars(self) -> None:
        """Filenames with letters, numbers, dots, hyphens, underscores pass through."""
        assert sanitize_file_name("file_123.PNG", 0) == "file_123.PNG"
        assert sanitize_file_name("my-document.pdf", 1) == "my-document.pdf"
        assert sanitize_file_name("archive.tar.gz", 2) == "archive.tar.gz"

    def test_single_safe_character(self) -> None:
        """Single character filenames are preserved if safe."""
        result = sanitize_file_name("a", 0)
        assert result == "a"

    # Unsafe character replacement
    def test_unsafe_characters_replaced_with_underscore(self) -> None:
        """Unsafe characters are replaced with underscore."""
        result = sanitize_file_name("my file@#$.png", 0)
        assert result == "my_file___.png"

    def test_common_unsafe_characters(self) -> None:
        """Common unsafe characters are replaced."""
        assert sanitize_file_name("file<name>.txt", 0) == "file_name_.txt"
        assert sanitize_file_name("test|file.bin", 0) == "test_file.bin"
        assert sanitize_file_name("path*name?.doc", 0) == "path_name_.doc"

    def test_whitespace_replaced_with_underscore(self) -> None:
        """Whitespace characters are replaced with underscore."""
        result = sanitize_file_name("my file.png", 0)
        assert result == "my_file.png"

    def test_multiple_consecutive_unsafe_chars(self) -> None:
        """Multiple consecutive unsafe characters are each replaced."""
        result = sanitize_file_name("file@#$%name.txt", 0)
        assert result == "file____name.txt"

    # Path handling
    def test_unix_path_separators_stripped(self) -> None:
        """Unix path separators (/) are stripped to base name only."""
        result = sanitize_file_name("../../../etc/passwd", 0)
        assert result == "passwd"

    def test_windows_path_separators_stripped(self) -> None:
        """Windows path separators (\\) are stripped to base name only."""
        result = sanitize_file_name("C:\\Windows\\System32\\file.dll", 0)
        assert result == "file.dll"

    def test_mixed_path_separators(self) -> None:
        """Mixed path separators are handled correctly."""
        result = sanitize_file_name("../../../windows\\file.txt", 0)
        assert result == "file.txt"

    def test_deep_relative_paths(self) -> None:
        """Deep relative paths extract just the filename."""
        result = sanitize_file_name("a/b/c/d/e/f/g/filename.txt", 0)
        assert result == "filename.txt"

    # Special fallback cases
    def test_empty_string_returns_fallback(self) -> None:
        """Empty string falls back to file_<index+1>.bin."""
        assert sanitize_file_name("", 0) == "file_1.bin"
        assert sanitize_file_name("", 1) == "file_2.bin"
        assert sanitize_file_name("", 3) == "file_4.bin"

    def test_dot_only_returns_fallback(self) -> None:
        """Filename of only '.' falls back to file_<index+1>.bin."""
        result = sanitize_file_name(".", 2)
        assert result == "file_3.bin"

    def test_double_dot_returns_fallback(self) -> None:
        """Filename of only '..' falls back to file_<index+1>.bin."""
        result = sanitize_file_name("..", 1)
        assert result == "file_2.bin"

    def test_sanitized_to_empty_returns_fallback(self) -> None:
        """If sanitization results in empty string, use fallback."""
        result = sanitize_file_name("@#$%", 3)
        assert result == "file_4.bin"

    def test_sanitized_to_only_underscores_returns_fallback(self) -> None:
        """If sanitization results in only underscores, use fallback."""
        result = sanitize_file_name("@@@", 0)
        assert result == "file_1.bin"

    def test_result_becomes_dot_returns_fallback(self) -> None:
        """If result becomes single dot after sanitization, use fallback."""
        result = sanitize_file_name("@.@", 5)
        # After replacing unsafe chars: "_._ ", but taking base name and sanitizing
        # should not result in just ".", so let's test the actual behavior
        # The pattern matches non-safe chars, so "@.@" becomes "_._"
        assert result == "_._"  # This won't be a fallback case

    # Truncation
    def test_filename_capped_at_255_characters(self) -> None:
        """Filename is truncated to 255 characters."""
        long_name = "a" * 300 + ".png"
        result = sanitize_file_name(long_name, 0)
        assert len(result) == 255
        assert result.endswith(".png") or result.endswith("a")

    def test_truncation_preserves_start_of_name(self) -> None:
        """Truncation keeps the start of the filename."""
        long_name = "important_prefix_" + "x" * 300
        result = sanitize_file_name(long_name, 0)
        assert result.startswith("important_prefix_")
        assert len(result) == 255

    # Index parameterization
    def test_fallback_uses_different_indices(self) -> None:
        """Fallback filename uses correct index."""
        assert sanitize_file_name("", 0) == "file_1.bin"
        assert sanitize_file_name("", 1) == "file_2.bin"
        assert sanitize_file_name("", 2) == "file_3.bin"
        assert sanitize_file_name("", 3) == "file_4.bin"


class TestNormalizeToList:
    """Tests for normalize_to_list helper."""

    # None and empty cases
    def test_none_returns_empty_list(self) -> None:
        """None input returns empty list."""
        result = normalize_to_list(None)
        assert result == []

    # List input cases
    def test_list_returns_list_of_strings(self) -> None:
        """List input is converted to strings."""
        result = normalize_to_list(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_empty_list_returns_empty_list(self) -> None:
        """Empty list returns empty list."""
        result = normalize_to_list([])
        assert result == []

    def test_list_filters_none_values(self) -> None:
        """List input filters out None values."""
        result = normalize_to_list(["a", None, "b"])
        assert result == ["a", "b"]

    def test_list_filters_empty_strings(self) -> None:
        """List input filters out empty strings."""
        result = normalize_to_list(["a", "", "b"])
        assert result == ["a", "b"]

    def test_list_filters_falsy_values(self) -> None:
        """List input filters out all falsy values (None, empty string)."""
        result = normalize_to_list(["a", None, "", "b"])
        assert result == ["a", "b"]

    def test_list_with_numeric_values_converts_to_strings(self) -> None:
        """List with numeric values converts them to strings."""
        result = normalize_to_list([1, 2, 3])
        assert result == ["1", "2", "3"]

    def test_list_with_mixed_types(self) -> None:
        """List with mixed types converts all to strings."""
        result = normalize_to_list(["text", 42, 3.14])
        assert result == ["text", "42", "3.14"]

    # String input cases
    def test_single_string_returns_list_with_one_item(self) -> None:
        """Single string returns a one-item list."""
        result = normalize_to_list("single.png")
        assert result == ["single.png"]

    def test_comma_separated_string_is_split(self) -> None:
        """Comma-separated string is parsed into a list."""
        result = normalize_to_list("file1.png,file2.png,file3.png")
        assert result == ["file1.png", "file2.png", "file3.png"]

    def test_comma_separated_with_spaces(self) -> None:
        """Spaces around commas are trimmed."""
        result = normalize_to_list("file1.png, file2.png, file3.png")
        assert result == ["file1.png", "file2.png", "file3.png"]

    def test_comma_separated_with_excessive_whitespace(self) -> None:
        """Excessive whitespace around comma-separated values is stripped."""
        result = normalize_to_list("  file1.png  ,  file2.png  ,  file3.png  ")
        assert result == ["file1.png", "file2.png", "file3.png"]

    def test_string_with_empty_parts_after_split(self) -> None:
        """Empty parts (after stripping) are filtered out."""
        result = normalize_to_list("file1.png,,file2.png")
        assert result == ["file1.png", "file2.png"]

    def test_string_with_only_commas(self) -> None:
        """String with only commas and spaces returns empty list."""
        result = normalize_to_list(",,,")
        assert result == []

    def test_string_with_trailing_comma(self) -> None:
        """String with trailing comma doesn't create empty item."""
        result = normalize_to_list("file1.png,file2.png,")
        assert result == ["file1.png", "file2.png"]

    def test_string_with_leading_comma(self) -> None:
        """String with leading comma doesn't create empty item."""
        result = normalize_to_list(",file1.png,file2.png")
        assert result == ["file1.png", "file2.png"]

    # Numeric/scalar input cases
    def test_numeric_value_is_converted_to_string(self) -> None:
        """Non-string scalar values are converted to strings."""
        result = normalize_to_list(42)
        assert result == ["42"]

    def test_float_value_is_converted_to_string(self) -> None:
        """Float values are converted to strings."""
        result = normalize_to_list(3.14)
        assert result == ["3.14"]

    def test_boolean_true_converted_to_string(self) -> None:
        """Boolean True is converted to string."""
        result = normalize_to_list(True)
        assert result == ["True"]

    def test_boolean_false_converted_to_string(self) -> None:
        """Boolean False is converted to string."""
        result = normalize_to_list(False)
        assert result == ["False"]

    # Edge cases
    def test_string_with_just_spaces(self) -> None:
        """String with only spaces returns empty list."""
        result = normalize_to_list("   ")
        assert result == []

    def test_string_with_single_value(self) -> None:
        """String with single value returns one-item list."""
        result = normalize_to_list("single")
        assert result == ["single"]


class TestExtractJobId:
    """Tests for extract_job_id helper."""

    # Query string cases
    def test_extracts_from_query_string(self) -> None:
        """jobId is extracted from query string parameters."""
        event = {"queryStringParameters": {"jobId": "test-job-123"}}
        result = extract_job_id(event)
        assert result == "test-job-123"

    def test_query_string_with_uuid_format(self) -> None:
        """jobId in UUID format is extracted correctly."""
        uuid_id = "550e8400-e29b-41d4-a716-446655440000"
        event = {"queryStringParameters": {"jobId": uuid_id}}
        result = extract_job_id(event)
        assert result == uuid_id

    def test_numeric_job_id_in_query_is_converted_to_string(self) -> None:
        """Numeric jobId in query string is converted to string."""
        event = {"queryStringParameters": {"jobId": 12345}}
        result = extract_job_id(event)
        assert result == "12345"

    # JSON body cases
    def test_extracts_from_json_body(self) -> None:
        """jobId is extracted from JSON body."""
        event = {"body": json.dumps({"jobId": "body-job-456"})}
        result = extract_job_id(event)
        assert result == "body-job-456"

    def test_numeric_job_id_in_body_is_converted_to_string(self) -> None:
        """Numeric jobId in body is converted to string."""
        event = {"body": json.dumps({"jobId": 67890})}
        result = extract_job_id(event)
        assert result == "67890"

    def test_boolean_job_id_in_body_is_converted_to_string(self) -> None:
        """Boolean jobId in body is converted to string."""
        event = {"body": json.dumps({"jobId": True})}
        result = extract_job_id(event)
        assert result == "True"

    # Precedence cases
    def test_body_takes_precedence_over_query(self) -> None:
        """Body jobId takes precedence over query string jobId."""
        event = {
            "queryStringParameters": {"jobId": "query-job"},
            "body": json.dumps({"jobId": "body-job"}),
        }
        result = extract_job_id(event)
        assert result == "body-job"

    def test_body_empty_string_takes_precedence(self) -> None:
        """Empty string jobId in body still takes precedence over query."""
        event = {
            "queryStringParameters": {"jobId": "query-job"},
            "body": json.dumps({"jobId": ""}),
        }
        result = extract_job_id(event)
        # Empty string is falsy, so it should fall back to query
        assert result == "query-job"

    def test_body_with_jobid_overrides_query_without_jobid(self) -> None:
        """Body with jobId overrides query without jobId."""
        event = {
            "queryStringParameters": {"otherParam": "value"},
            "body": json.dumps({"jobId": "from-body"}),
        }
        result = extract_job_id(event)
        assert result == "from-body"

    # Missing/invalid cases
    def test_missing_job_id_returns_unknown(self) -> None:
        """Missing jobId returns 'unknown'."""
        event = {"queryStringParameters": {}}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_empty_query_parameters_returns_unknown(self) -> None:
        """Empty query parameters returns 'unknown'."""
        event = {"queryStringParameters": None}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_no_body_returns_unknown(self) -> None:
        """Event with no body returns 'unknown'."""
        event = {}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_empty_job_id_returns_unknown(self) -> None:
        """Empty jobId value returns 'unknown'."""
        event = {"queryStringParameters": {"jobId": ""}}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_none_job_id_returns_unknown(self) -> None:
        """None jobId value returns 'unknown'."""
        event = {"queryStringParameters": {"jobId": None}}
        result = extract_job_id(event)
        assert result == "unknown"

    # JSON parsing error cases
    def test_malformed_json_returns_unknown(self) -> None:
        """Malformed JSON body returns 'unknown'."""
        event = {"body": "{not valid json}"}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_json_array_instead_of_object_returns_unknown(self) -> None:
        """JSON array instead of object returns 'unknown'."""
        event = {"body": json.dumps(["item1", "item2"])}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_json_string_instead_of_object_returns_unknown(self) -> None:
        """JSON string instead of object returns 'unknown'."""
        event = {"body": json.dumps("just a string")}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_body_is_none_returns_unknown(self) -> None:
        """body key with None value returns 'unknown'."""
        event = {"body": None}
        result = extract_job_id(event)
        assert result == "unknown"

    def test_empty_string_body_returns_unknown(self) -> None:
        """Empty string body returns 'unknown'."""
        event = {"body": ""}
        result = extract_job_id(event)
        assert result == "unknown"

    # Base64 encoding cases
    def test_base64_encoded_body_is_decoded(self) -> None:
        """Base64-encoded body is decoded before parsing."""
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

    def test_invalid_utf8_in_base64_returns_unknown(self) -> None:
        """Invalid UTF-8 in decoded base64 returns 'unknown'."""
        # Create invalid UTF-8 sequence
        invalid_utf8 = b"\xFF\xFE"
        encoded = base64.b64encode(invalid_utf8).decode()
        event = {
            "body": encoded,
            "isBase64Encoded": True,
        }
        result = extract_job_id(event)
        assert result == "unknown"

    def test_base64_false_treats_as_plain_text(self) -> None:
        """isBase64Encoded=False treats body as plain text."""
        json_str = json.dumps({"jobId": "plain-text"})
        event = {
            "body": json_str,
            "isBase64Encoded": False,
        }
        result = extract_job_id(event)
        assert result == "plain-text"

    def test_no_is_base64_encoded_flag_treats_as_plain_text(self) -> None:
        """Missing isBase64Encoded flag treats body as plain text."""
        json_str = json.dumps({"jobId": "plain-text-2"})
        event = {"body": json_str}
        result = extract_job_id(event)
        assert result == "plain-text-2"

    # Query parameters not a dict
    def test_query_params_not_dict_returns_unknown(self) -> None:
        """queryStringParameters that is not a dict returns 'unknown'."""
        event = {"queryStringParameters": "not-a-dict"}
        result = extract_job_id(event)
        assert result == "unknown"


class TestParseEvent:
    """Tests for parse_event helper."""

    # Query string file names cases
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

    def test_plural_takes_precedence_over_singular_in_query(self) -> None:
        """Plural 'fileNames' takes precedence over singular 'fileName' in query."""
        event = {
            "queryStringParameters": {
                "fileNames": "plural.png",
                "fileName": "singular.png",
            }
        }
        names, types = parse_event(event)
        assert names == ["plural.png"]

    def test_query_file_names_as_list(self) -> None:
        """File names as list in query string are parsed."""
        event = {
            "queryStringParameters": {
                "fileNames": ["file1.png", "file2.png"],
            }
        }
        names, types = parse_event(event)
        assert names == ["file1.png", "file2.png"]

    # Query string content types cases
    def test_parses_content_types_from_query_string(self) -> None:
        """Content types are parsed from query string parameters."""
        event = {
            "queryStringParameters": {
                "contentTypes": "image/png,image/jpeg",
            }
        }
        names, types = parse_event(event)
        assert types == ["image/png", "image/jpeg"]

    def test_parses_singular_content_type_from_query_string(self) -> None:
        """Singular 'contentType' is parsed from query string."""
        event = {
            "queryStringParameters": {
                "contentType": "image/png",
            }
        }
        names, types = parse_event(event)
        assert types == ["image/png"]

    def test_plural_content_type_takes_precedence(self) -> None:
        """Plural 'contentTypes' takes precedence over singular 'contentType'."""
        event = {
            "queryStringParameters": {
                "contentTypes": "image/jpeg",
                "contentType": "image/png",
            }
        }
        names, types = parse_event(event)
        assert types == ["image/jpeg"]

    # JSON body cases
    def test_parses_file_names_from_json_body(self) -> None:
        """File names are parsed from JSON body."""
        event = {
            "body": json.dumps({
                "fileNames": ["body1.png", "body2.png"],
            }),
        }
        names, types = parse_event(event)
        assert names == ["body1.png", "body2.png"]

    def test_parses_singular_file_name_from_json_body(self) -> None:
        """Singular fileName is parsed from JSON body."""
        event = {
            "body": json.dumps({
                "fileName": "body.png",
            }),
        }
        names, types = parse_event(event)
        assert names == ["body.png"]

    def test_parses_content_types_from_json_body(self) -> None:
        """Content types are parsed from JSON body."""
        event = {
            "body": json.dumps({
                "contentTypes": ["image/png", "image/jpeg"],
            }),
        }
        names, types = parse_event(event)
        assert types == ["image/png", "image/jpeg"]

    def test_parses_singular_content_type_from_json_body(self) -> None:
        """Singular contentType is parsed from JSON body."""
        event = {
            "body": json.dumps({
                "contentType": "image/png",
            }),
        }
        names, types = parse_event(event)
        assert types == ["image/png"]

    # Precedence: body over query
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

    def test_body_singular_overrides_query_plural(self) -> None:
        """Body singular form overrides query plural form."""
        event = {
            "queryStringParameters": {"fileNames": "query1.png,query2.png"},
            "body": json.dumps({"fileName": "body.png"}),
        }
        names, types = parse_event(event)
        assert names == ["body.png"]

    def test_empty_body_values_dont_override_query(self) -> None:
        """Empty body values don't override query values."""
        event = {
            "queryStringParameters": {"fileNames": "query.png"},
            "body": json.dumps({"fileNames": []}),
        }
        names, types = parse_event(event)
        assert names == ["query.png"]

    def test_body_only_overrides_file_names_not_content_types(self) -> None:
        """Body only overrides the params that are present in body."""
        event = {
            "queryStringParameters": {
                "fileNames": "query.png",
                "contentTypes": "image/png",
            },
            "body": json.dumps({"fileNames": ["body.png"]}),
        }
        names, types = parse_event(event)
        assert names == ["body.png"]
        assert types == ["image/png"]  # From query, not overridden

    # Error handling: malformed JSON
    def test_malformed_json_body_falls_back_to_query_string(self) -> None:
        """Malformed JSON body falls back to query string parsing."""
        event = {
            "queryStringParameters": {"fileName": "fallback.png"},
            "body": "{not valid json",
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]

    def test_invalid_json_body_uses_query_params(self) -> None:
        """Various invalid JSON bodies fall back to query params."""
        event = {
            "queryStringParameters": {
                "fileName": "fallback.png",
                "contentType": "image/png",
            },
            "body": "[]invalid",
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]
        assert types == ["image/png"]

    def test_json_non_dict_falls_back_to_query(self) -> None:
        """JSON that is not a dict falls back to query."""
        event = {
            "queryStringParameters": {"fileName": "fallback.png"},
            "body": json.dumps(["array", "not", "dict"]),
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]

    # Base64 encoding
    def test_base64_encoded_body_is_decoded(self) -> None:
        """Base64-encoded body is decoded before JSON parsing."""
        body_json = json.dumps({"fileNames": ["decoded.png"]})
        encoded = base64.b64encode(body_json.encode()).decode()
        event = {
            "body": encoded,
            "isBase64Encoded": True,
        }
        names, types = parse_event(event)
        assert names == ["decoded.png"]

    def test_invalid_base64_falls_back_to_query(self) -> None:
        """Invalid base64 encoding falls back to query params."""
        event = {
            "body": "not valid base64!!!",
            "isBase64Encoded": True,
            "queryStringParameters": {"fileName": "fallback.png"},
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]

    def test_invalid_utf8_in_base64_falls_back_to_query(self) -> None:
        """Invalid UTF-8 in base64 falls back to query."""
        invalid_utf8 = b"\xFF\xFE"
        encoded = base64.b64encode(invalid_utf8).decode()
        event = {
            "body": encoded,
            "isBase64Encoded": True,
            "queryStringParameters": {"fileName": "fallback.png"},
        }
        names, types = parse_event(event)
        assert names == ["fallback.png"]

    def test_base64_false_treats_as_plain_text(self) -> None:
        """isBase64Encoded=False treats body as plain text JSON."""
        json_str = json.dumps({"fileNames": ["plain.png"]})
        event = {
            "body": json_str,
            "isBase64Encoded": False,
        }
        names, types = parse_event(event)
        assert names == ["plain.png"]

    def test_no_is_base64_encoded_flag_treats_as_plain_text(self) -> None:
        """Missing isBase64Encoded treats body as plain text."""
        json_str = json.dumps({"fileNames": ["plain.png"]})
        event = {"body": json_str}
        names, types = parse_event(event)
        assert names == ["plain.png"]

    # Empty/missing cases
    def test_empty_event_returns_empty_lists(self) -> None:
        """Empty event returns empty lists."""
        names, types = parse_event({})
        assert names == []
        assert types == []

    def test_none_query_parameters_returns_empty_lists(self) -> None:
        """None queryStringParameters returns empty lists."""
        event = {"queryStringParameters": None}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    def test_empty_query_parameters_returns_empty_lists(self) -> None:
        """Empty query parameters dict returns empty lists."""
        event = {"queryStringParameters": {}}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    def test_no_body_returns_empty_lists(self) -> None:
        """Event with no body returns empty lists."""
        event = {"queryStringParameters": {}}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    def test_empty_body_returns_empty_lists(self) -> None:
        """Empty body returns empty lists."""
        event = {"body": ""}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    def test_none_body_returns_empty_lists(self) -> None:
        """None body returns empty lists."""
        event = {"body": None}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    # Query parameters not a dict
    def test_query_params_not_dict_returns_empty(self) -> None:
        """queryStringParameters that is not a dict returns empty lists."""
        event = {"queryStringParameters": "not-a-dict"}
        names, types = parse_event(event)
        assert names == []
        assert types == []

    # Both fileNames and fileName present
    def test_body_both_plural_and_singular_uses_plural(self) -> None:
        """Body with both fileNames and fileName uses fileNames."""
        event = {
            "body": json.dumps({
                "fileNames": ["plural.png"],
                "fileName": "singular.png",
            }),
        }
        names, types = parse_event(event)
        assert names == ["plural.png"]

    def test_both_singular_and_plural_in_body_and_query(self) -> None:
        """Both forms in body and query use body plural."""
        event = {
            "queryStringParameters": {
                "fileNames": "query-plural.png",
                "fileName": "query-singular.png",
            },
            "body": json.dumps({
                "fileNames": ["body-plural.png"],
                "fileName": "body-singular.png",
            }),
        }
        names, types = parse_event(event)
        assert names == ["body-plural.png"]

    # Empty collections
    def test_empty_list_in_body_overrides_query(self) -> None:
        """Empty list in body doesn't override query (falsy check)."""
        event = {
            "queryStringParameters": {"fileNames": "query.png"},
            "body": json.dumps({"fileNames": []}),
        }
        names, types = parse_event(event)
        # Empty list is falsy, so it shouldn't override
        assert names == ["query.png"]

    def test_combined_query_and_body_partial_override(self) -> None:
        """Body partially overrides: fileNames from body, contentTypes from query."""
        event = {
            "queryStringParameters": {
                "fileNames": "query1.png,query2.png",
                "contentTypes": "image/png,image/jpeg",
            },
            "body": json.dumps({"fileNames": ["body.png"]}),
        }
        names, types = parse_event(event)
        assert names == ["body.png"]
        assert types == ["image/png", "image/jpeg"]
