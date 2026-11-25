"""Tests for the start-job Lambda.

Covers caller identity extraction, jobId validation (canonical UUID) from path parameters,
S3 image listing, the owner-scoped DynamoDB status lookup, the owner-conditioned status
update, SQS message sending, transport-failure (`BotoCoreError`) handling, revert-on-failure,
and the full orchestration flow.
"""

import datetime
import json
import logging
import os
from typing import Any, Dict, Iterator

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from moto import mock_aws
from unittest.mock import patch

from handler import (
    _get_client,
    get_caller_user_id,
    get_owned_job_status,
    lambda_handler,
    list_uploaded_images,
    parse_instruction,
    parse_job_id,
    revert_job_item,
    trigger_guide_creation,
    update_job_item,
)

BUCKET_NAME = "test-upload-bucket"
TABLE_NAME = "test-jobs-table"
QUEUE_URL = "https://sqs.eu-central-1.amazonaws.com/123456789/test-queue"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_JOB_ID = "6f1c2d3e-4a5b-4c6d-8e7f-900112233445"
USER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_USER_ID = "ffffffff-1111-4222-8333-444444444444"


def with_rest_claims(event: Dict[str, Any], user_id: str = USER_ID) -> Dict[str, Any]:
    """Return a copy of `event` as a REST API Cognito authorizer would deliver it.

    Args:
        event: The base event; it is not mutated.
        user_id: The Cognito `sub` placed in `requestContext.authorizer.claims`.

    Returns:
        A new event dict carrying `requestContext.authorizer.claims.sub`.
    """
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user_id}}}}


def with_jwt_claims(event: Dict[str, Any], user_id: str = USER_ID) -> Dict[str, Any]:
    """Return a copy of `event` as an HTTP API JWT authorizer would deliver it.

    Args:
        event: The base event; it is not mutated.
        user_id: The Cognito `sub` placed in `requestContext.authorizer.jwt.claims`.

    Returns:
        A new event dict carrying `requestContext.authorizer.jwt.claims.sub`.
    """
    return {
        **event,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": user_id}}}},
    }


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Iterator[None]:
    """Reset the boto3 client cache so a client from one moto mock cannot leak into the next."""
    _get_client.cache_clear()
    yield
    _get_client.cache_clear()


class TestParseJobId:
    """Tests for the `parse_job_id` request-validation helper."""

    def test_valid_job_id_from_path_parameters(self) -> None:
        """Valid `jobId` in pathParameters should be returned as-is."""
        event = {"pathParameters": {"jobId": JOB_ID}}
        job_id, error_response = parse_job_id(event)
        assert job_id == JOB_ID
        assert error_response is None

    def test_job_id_with_whitespace_is_trimmed(self) -> None:
        """`jobId` with leading/trailing whitespace should be trimmed."""
        event = {"pathParameters": {"jobId": f"  {JOB_ID}  "}}
        job_id, error_response = parse_job_id(event)
        assert job_id == JOB_ID
        assert error_response is None

    def test_missing_path_parameters_returns_400(self) -> None:
        """No `pathParameters` key should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body["error"] == "InvalidRequest"
        assert body["message"] == "jobId is required"

    def test_missing_job_id_in_path_parameters_returns_400(self) -> None:
        """No `jobId` in pathParameters should return a 400 InvalidRequest error."""
        event = {"pathParameters": {}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_null_path_parameters_returns_400(self) -> None:
        """`pathParameters` set to None should return a 400 InvalidRequest error."""
        event = {"pathParameters": None}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_empty_job_id_returns_400(self) -> None:
        """Empty `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": ""}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_whitespace_only_job_id_returns_400(self) -> None:
        """Whitespace-only `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": "   "}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_non_string_job_id_returns_400(self) -> None:
        """Non-string `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": 12345}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400


    @pytest.mark.parametrize(
        "job_id",
        [
            "not-a-uuid",
            "abc123",
            JOB_ID.upper(),
            JOB_ID.replace("-", ""),
            f"{{{JOB_ID}}}",
            f"urn:uuid:{JOB_ID}",
            f"{JOB_ID}/../other-job",
            f"../{JOB_ID}",
            "123e4567-e89b-12d3-a456-42661417400",
        ],
    )
    def test_non_canonical_uuid_returns_400(self, job_id: str) -> None:
        """Only a canonical lowercase UUID may address a job; anything else is a 400."""
        parsed_job_id, error_response = parse_job_id({"pathParameters": {"jobId": job_id}})

        assert parsed_job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body == {"error": "InvalidRequest", "message": "jobId must be a canonical UUID"}


class TestListUploadedImages:
    """Tests for the `list_uploaded_images` S3 listing helper."""

    @mock_aws
    def test_lists_single_image(self) -> None:
        """List exactly 1 image from S3."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"fake image data",
            )

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 1
        assert all(img.startswith(f"s3://{BUCKET_NAME}/") for img in images)

    @mock_aws
    def test_excludes_zero_byte_objects(self) -> None:
        """Zero-byte S3 objects (folder markers) should be excluded."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"

        for i in range(3):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"fake image data",
            )
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=f"{prefix}.folder_marker",
            Body=b"",
        )

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 3

    @mock_aws
    def test_returns_sorted_urls(self) -> None:
        """Images should be returned as sorted S3 URLs."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        for i in [2, 0, 3, 1]:
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        expected_keys = [
            f"s3://{BUCKET_NAME}/{prefix}image_0.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_1.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_2.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_3.jpg",
        ]
        assert images == expected_keys

    @mock_aws
    def test_lists_all_images_without_enforcing_count(self) -> None:
        """Listing returns every image found; the count rule is enforced by the handler."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        s3.put_object(Bucket=BUCKET_NAME, Key=f"{prefix}image_0.jpg", Body=b"data")
        s3.put_object(Bucket=BUCKET_NAME, Key=f"{prefix}image_1.jpg", Body=b"data")

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 2

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                list_uploaded_images(JOB_ID)


class TestGetCallerUserId:
    """Tests for the `get_caller_user_id` authorizer-claims helper."""

    def test_reads_sub_from_rest_authorizer_claims(self) -> None:
        """The REST API Cognito authorizer shape should yield the `sub` claim."""
        assert get_caller_user_id(with_rest_claims({})) == USER_ID

    def test_reads_sub_from_http_api_jwt_claims(self) -> None:
        """The HTTP API JWT authorizer shape should yield the `sub` claim."""
        assert get_caller_user_id(with_jwt_claims({})) == USER_ID

    def test_rest_claims_take_precedence_over_jwt_claims(self) -> None:
        """When both shapes are present, the REST `claims` are the source of truth."""
        event = {
            "requestContext": {
                "authorizer": {
                    "claims": {"sub": USER_ID},
                    "jwt": {"claims": {"sub": OTHER_USER_ID}},
                }
            }
        }

        assert get_caller_user_id(event) == USER_ID

    def test_strips_surrounding_whitespace_from_sub(self) -> None:
        """A `sub` padded with whitespace should be returned trimmed."""
        assert get_caller_user_id(with_rest_claims({}, user_id=f"  {USER_ID}  ")) == USER_ID

    @pytest.mark.parametrize(
        "event",
        [
            {},
            {"requestContext": None},
            {"requestContext": {"authorizer": None}},
            {"requestContext": {"authorizer": {}}},
            {"requestContext": {"authorizer": {"claims": {}}}},
            {"requestContext": {"authorizer": {"jwt": {}}}},
            {"requestContext": {"authorizer": {"claims": "not-a-dict"}}},
            {"requestContext": {"authorizer": {"claims": {"sub": None}}}},
            {"requestContext": {"authorizer": {"claims": {"sub": 12345}}}},
            {"requestContext": {"authorizer": {"claims": {"sub": ""}}}},
            {"requestContext": {"authorizer": {"claims": {"sub": "   "}}}},
            {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": " "}}}}},
        ],
    )
    def test_returns_none_when_caller_identity_is_unavailable(self, event: dict) -> None:
        """Missing, malformed, or blank claims must yield `None`, never a guessed identity."""
        assert get_caller_user_id(event) is None


class TestGetOwnedJobStatus:
    """Tests for the `get_owned_job_status` owner-scoped DynamoDB lookup helper."""

    @mock_aws
    def test_returns_none_when_job_is_owned_by_another_user(self) -> None:
        """Another user's job must look exactly like a missing job (`None`)."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": OTHER_USER_ID},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_owned_job_status(JOB_ID, USER_ID)

        assert status is None

    @mock_aws
    def test_returns_none_for_legacy_item_without_owner(self) -> None:
        """A job created before ownership was recorded is not revealed to any caller."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_owned_job_status(JOB_ID, USER_ID)

        assert status is None

    @mock_aws
    def test_returns_status_when_caller_owns_job(self) -> None:
        """The owner sees the raw stored status (not converted to the JobStatus enum)."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "PAINTED"},
                "userId": {"S": USER_ID},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_owned_job_status(JOB_ID, USER_ID)

        assert status == "PAINTED"

    @mock_aws
    def test_read_timeout_propagates_as_botocore_error(self) -> None:
        """A transport failure is raised for the handler to turn into a 500."""
        dynamodb_client = boto3.client("dynamodb")
        _create_jobs_table(dynamodb_client)
        timeout = ReadTimeoutError(endpoint_url="https://dynamodb.eu-central-1.amazonaws.com")

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with patch("handler._get_client") as mock_get_client:
                mock_get_client.return_value.get_item.side_effect = timeout
                with pytest.raises(ReadTimeoutError):
                    get_owned_job_status(JOB_ID, USER_ID)

    @mock_aws
    def test_returns_status_when_job_exists(self) -> None:
        """Should return job status when job exists in DynamoDB."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_owned_job_status(JOB_ID, USER_ID)

        assert status == "UPLOADED"

    @mock_aws
    def test_returns_none_when_job_not_found(self) -> None:
        """Should return None when job does not exist in DynamoDB."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_owned_job_status(JOB_ID, USER_ID)

        assert status is None

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                get_owned_job_status(JOB_ID, USER_ID)


class TestParseInstruction:
    """Tests for the `parse_instruction` optional-body helper."""

    def test_returns_instruction_from_body(self) -> None:
        event = {"body": json.dumps({"instruction": "  Paint the cloak red  "})}
        assert parse_instruction(event) == ("Paint the cloak red", None)

    @pytest.mark.parametrize(
        "body",
        [None, "", "   ", "{}", json.dumps({"instruction": None}),
         json.dumps({"instruction": ""}), json.dumps({"instruction": "   "})],
    )
    def test_returns_none_when_instruction_absent_or_blank(self, body) -> None:
        assert parse_instruction({"body": body}) == (None, None)

    def test_returns_none_when_event_has_no_body(self) -> None:
        assert parse_instruction({}) == (None, None)

    @pytest.mark.parametrize("body", ["not json", "[1, 2]", json.dumps({"instruction": 5})])
    def test_returns_400_for_malformed_body_or_non_string_instruction(self, body) -> None:
        instruction, error_response = parse_instruction({"body": body})
        assert instruction is None
        assert error_response["statusCode"] == 400


class TestUpdateJobItem:
    """Tests for the `update_job_item` DynamoDB update helper."""

    @mock_aws
    def test_updates_status_to_in_progress(self) -> None:
        """Should update job status to IN_PROGRESS when status is UPLOADED."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID, USER_ID)

        response = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"jobId": {"S": JOB_ID}},
        )
        assert response["Item"]["jobStatus"]["S"] == "IN_PROGRESS"

    @mock_aws
    def test_stores_instruction_when_provided(self) -> None:
        """Should save the instruction on the job item alongside the status."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}, "userId": {"S": USER_ID}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID, USER_ID, "Paint the cloak red")

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "IN_PROGRESS"
        assert item["instruction"]["S"] == "Paint the cloak red"

    @mock_aws
    def test_does_not_store_instruction_when_not_provided(self) -> None:
        """Should leave the item without an `instruction` attribute."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}, "userId": {"S": USER_ID}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID, USER_ID, None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert "instruction" not in item

    @mock_aws
    def test_fails_when_job_not_found(self) -> None:
        """Should fail when job not found."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID, USER_ID)
            assert (
                exc_info.value.response["Error"]["Code"]
                == "ConditionalCheckFailedException"
            )

    @mock_aws
    def test_fails_when_job_is_owned_by_another_user(self) -> None:
        """A non-owner's start must lose the conditional write and leave the job untouched."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": OTHER_USER_ID},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID, USER_ID, "Paint the cloak red")

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        item = _get_job_item(dynamodb)
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in item

    @mock_aws
    def test_fails_for_legacy_item_without_owner(self) -> None:
        """An item without `userId` cannot be started by anyone through the conditional write."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID, USER_ID)

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"

    @mock_aws
    def test_fails_when_status_not_uploaded(self) -> None:
        """Should fail when job status is not UPLOADED."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID, USER_ID)
            assert (
                exc_info.value.response["Error"]["Code"]
                == "ConditionalCheckFailedException"
            )

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                update_job_item(JOB_ID, USER_ID)


class TestTriggerGuideCreation:
    """Tests for the `trigger_guide_creation` SQS message sending helper."""

    @mock_aws
    def test_sends_sqs_message(self) -> None:
        """Should send jobId message to SQS queue."""
        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        with patch.dict(os.environ, {"PAINT_QUEUE_URL": queue_url}):
            trigger_guide_creation(JOB_ID)

        messages = sqs.receive_message(QueueUrl=queue_url)
        assert len(messages["Messages"]) == 1
        body = json.loads(messages["Messages"][0]["Body"])
        assert body["jobId"] == JOB_ID

    def test_missing_queue_url_env_var_raises_key_error(self) -> None:
        """Missing PAINT_QUEUE_URL env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                trigger_guide_creation(JOB_ID)


def _create_jobs_table(dynamodb) -> None:
    """Create the moto jobs table keyed by `jobId`."""
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _get_job_item(dynamodb) -> dict:
    """Return the stored job item for `JOB_ID`."""
    return dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]


class TestRevertJobItem:
    """Tests for `revert_job_item`, which undoes `update_job_item` after a failed trigger."""

    @mock_aws
    def test_reverts_in_progress_job_and_removes_its_instruction(self) -> None:
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "userId": {"S": USER_ID},
                "instruction": {"S": "Paint the cloak red"},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            revert_job_item(JOB_ID, "Paint the cloak red")

        item = _get_job_item(dynamodb)
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in item
        assert "updatedAt" in item

    @mock_aws
    def test_keeps_instruction_it_did_not_write(self) -> None:
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "userId": {"S": USER_ID},
                "instruction": {"S": "earlier instruction"},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            revert_job_item(JOB_ID, None)

        item = _get_job_item(dynamodb)
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert item["instruction"]["S"] == "earlier instruction"

    @mock_aws
    def test_fails_when_status_is_no_longer_in_progress(self) -> None:
        """A status set by another writer (e.g. PAINTED) must not be overwritten."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "PAINTED"}, "userId": {"S": USER_ID}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                revert_job_item(JOB_ID)

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "PAINTED"

    def test_missing_table_env_var_raises_key_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                revert_job_item(JOB_ID)


class TestLambdaHandlerTriggerFailure:
    """A failed SQS send must not leave the job stuck in IN_PROGRESS."""

    SQS_ERROR = ClientError(
        {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
        "SendMessage",
    )

    def _arrange_uploaded_job(self) -> Any:
        """Create one uploaded image and an UPLOADED job; return the DynamoDB client."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        s3.put_object(Bucket=BUCKET_NAME, Key=f"uploads/{JOB_ID}/image_0.jpg", Body=b"data")
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}, "userId": {"S": USER_ID}},
        )
        return dynamodb

    @mock_aws
    def test_sqs_client_error_reverts_job_so_retry_succeeds(self) -> None:
        dynamodb = self._arrange_uploaded_job()
        queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": queue_url,
        }
        event = with_rest_claims(
            {
                "pathParameters": {"jobId": JOB_ID},
                "body": json.dumps({"instruction": "Paint the cloak red"}),
            }
        )

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=self.SQS_ERROR):
                failed = lambda_handler(event, None)
            reverted_item = _get_job_item(dynamodb)
            retried = lambda_handler(with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None)

        assert failed["statusCode"] == 500
        assert reverted_item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in reverted_item
        assert retried["statusCode"] == 200
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "IN_PROGRESS"

    @mock_aws
    def test_missing_queue_url_reverts_job(self) -> None:
        dynamodb = self._arrange_uploaded_job()
        env = {"JOBS_TABLE_NAME": TABLE_NAME, "UPLOAD_BUCKET_NAME": BUCKET_NAME}

        with patch.dict(os.environ, env):
            os.environ.pop("PAINT_QUEUE_URL", None)
            response = lambda_handler(with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None)

        assert response["statusCode"] == 500
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"

    @mock_aws
    def test_revert_failure_is_logged_and_original_500_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._arrange_uploaded_job()
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": QUEUE_URL,
        }
        revert_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
            "UpdateItem",
        )

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=self.SQS_ERROR):
                with patch("handler.revert_job_item", side_effect=revert_error):
                    with caplog.at_level(logging.ERROR, logger="handler"):
                        response = lambda_handler(
                            with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None
                        )

        assert response["statusCode"] == 500
        assert json.loads(response["body"])["message"] == "Failed to trigger guide creation."
        revert_records = [r for r in caplog.records if r.stage == "revert_status_error"]
        assert len(revert_records) == 1
        assert revert_records[0].jobId == JOB_ID
        assert revert_records[0].exc_info is not None

    @mock_aws
    def test_revert_skipped_when_job_already_moved_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If another writer changed the status, the revert loses and logs a warning."""
        dynamodb = self._arrange_uploaded_job()
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": QUEUE_URL,
        }

        def _fail_after_status_moves_on(job_id: str) -> None:
            """Simulate another writer moving the job on before the send fails."""
            dynamodb.update_item(
                TableName=TABLE_NAME,
                Key={"jobId": {"S": job_id}},
                UpdateExpression="SET jobStatus = :status",
                ExpressionAttributeValues={":status": {"S": "FAILED"}},
            )
            raise self.SQS_ERROR

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=_fail_after_status_moves_on):
                with caplog.at_level(logging.WARNING, logger="handler"):
                    response = lambda_handler(
                        with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None
                    )

        assert response["statusCode"] == 500
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "FAILED"
        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning.stage == "revert_status"

    @mock_aws
    @pytest.mark.parametrize(
        "transport_error",
        [
            ConnectTimeoutError(endpoint_url="https://sqs.eu-central-1.amazonaws.com"),
            ReadTimeoutError(endpoint_url="https://sqs.eu-central-1.amazonaws.com"),
        ],
        ids=["connect-timeout", "read-timeout"],
    )
    def test_sqs_transport_error_reverts_job_so_retry_succeeds(
        self, transport_error: Exception
    ) -> None:
        """An ambiguous SQS timeout still reverts, so the client's retry is not stuck on 409."""
        dynamodb = self._arrange_uploaded_job()
        queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": queue_url,
        }
        event = with_rest_claims(
            {
                "pathParameters": {"jobId": JOB_ID},
                "body": json.dumps({"instruction": "Paint the cloak red"}),
            }
        )

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=transport_error):
                failed = lambda_handler(event, None)
            reverted_item = _get_job_item(dynamodb)
            retried = lambda_handler(with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None)

        assert failed["statusCode"] == 500
        assert json.loads(failed["body"])["message"] == "Failed to trigger guide creation."
        assert reverted_item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in reverted_item
        assert retried["statusCode"] == 200

    @mock_aws
    def test_revert_transport_failure_is_logged_and_original_500_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A transport failure during the revert is logged and never replaces the 500."""
        self._arrange_uploaded_job()
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": QUEUE_URL,
        }
        revert_error = ReadTimeoutError(endpoint_url="https://dynamodb.eu-central-1.amazonaws.com")

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=self.SQS_ERROR):
                with patch("handler.revert_job_item", side_effect=revert_error):
                    with caplog.at_level(logging.ERROR, logger="handler"):
                        response = lambda_handler(
                            with_rest_claims({"pathParameters": {"jobId": JOB_ID}}), None
                        )

        assert response["statusCode"] == 500
        assert json.loads(response["body"])["message"] == "Failed to trigger guide creation."
        revert_records = [r for r in caplog.records if r.stage == "revert_status_error"]
        assert len(revert_records) == 1
        assert revert_records[0].jobId == JOB_ID
        assert revert_records[0].exc_info is not None


@pytest.fixture
def env_vars() -> dict:
    """Fixture providing standard environment variables."""
    return {
        "JOBS_TABLE_NAME": TABLE_NAME,
        "UPLOAD_BUCKET_NAME": BUCKET_NAME,
        "PAINT_QUEUE_URL": QUEUE_URL,
    }


class TestLambdaHandlerExceptions:
    """Tests for exception handling paths in lambda_handler."""

    def test_dynamodb_client_error_on_get_job_status_returns_500(
        self, env_vars
    ) -> None:
        """DynamoDB ClientError when getting job status should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_owned_job_status") as mock_get:
                mock_get.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "Rate exceeded",
                        }
                    },
                    "GetItem",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert body["error"] == "InternalError"
                assert "Failed to check job status" in body["message"]

    def test_client_error_is_logged_with_traceback_and_not_leaked(
        self, env_vars, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AWS failure details go to the log record only, never into the response body."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "GetItem",
        )

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_owned_job_status", side_effect=error):
                with caplog.at_level(logging.ERROR, logger="handler"):
                    response = lambda_handler(event, None)

        [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.stage == "get_status"
        assert record.jobId == JOB_ID
        assert record.exc_info is not None
        assert "Throttling" not in response["body"]
        assert "Rate exceeded" not in response["body"]

    def test_dynamodb_key_error_on_get_job_status_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME env var should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_TABLE_NAME", None)

            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "DynamoDB table name" in body["message"]

    @mock_aws
    def test_s3_client_error_on_list_images_returns_500(
        self, env_vars
    ) -> None:
        """S3 ClientError when listing images should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        with patch.dict(os.environ, env_vars):
            with patch("handler.list_uploaded_images") as mock_list:
                mock_list.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "NoSuchBucket",
                            "Message": "Bucket not found",
                        }
                    },
                    "ListObjectsV2",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to list uploaded images" in body["message"]

    @mock_aws
    def test_s3_key_error_on_list_images_returns_500(
        self, env_vars
    ) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        env_vars_copy = env_vars.copy()
        env_vars_copy.pop("UPLOAD_BUCKET_NAME", None)

        with patch.dict(os.environ, env_vars_copy):
            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "S3 bucket name" in body["message"]

    @mock_aws
    def test_dynamodb_client_error_on_update_job_returns_500(
        self, env_vars
    ) -> None:
        """DynamoDB ClientError on update_job_item should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        with patch.dict(os.environ, env_vars):
            with patch("handler.update_job_item") as mock_update:
                mock_update.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "AccessDeniedException",
                            "Message": "Access denied",
                        }
                    },
                    "UpdateItem",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to record job" in body["message"]

    @mock_aws
    def test_dynamodb_key_error_on_update_job_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME on update should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_TABLE_NAME", None)

            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]

    @mock_aws
    def test_sqs_client_error_on_trigger_returns_500(
        self, env_vars
    ) -> None:
        """SQS ClientError on trigger_guide_creation should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        with patch.dict(os.environ, env_vars):
            with patch("handler.trigger_guide_creation") as mock_trigger:
                mock_trigger.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "QueueDoesNotExist",
                            "Message": "Queue not found",
                        }
                    },
                    "SendMessage",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to trigger guide creation" in body["message"]

    @mock_aws
    def test_sqs_key_error_on_trigger_returns_500(
        self, env_vars
    ) -> None:
        """Missing PAINT_QUEUE_URL on trigger should return 500."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )

        env_vars_copy = env_vars.copy()
        env_vars_copy.pop("PAINT_QUEUE_URL", None)

        with patch.dict(os.environ, env_vars_copy):
            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "SQS queue URL" in body["message"]

    def test_transport_error_on_get_job_status_returns_500_without_leaking_details(
        self, env_vars, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A timed-out DynamoDB read is a 500 with a fixed message; the endpoint stays in logs."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        timeout = ConnectTimeoutError(endpoint_url="https://dynamodb.eu-central-1.amazonaws.com")

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_owned_job_status", side_effect=timeout):
                with caplog.at_level(logging.ERROR, logger="handler"):
                    response = lambda_handler(event, None)

        assert response["statusCode"] == 500
        assert json.loads(response["body"]) == {
            "error": "InternalError",
            "message": "Failed to check job status.",
        }
        assert "amazonaws.com" not in response["body"]
        [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.stage == "get_status"
        assert record.exc_info is not None

    def test_transport_error_on_list_images_returns_500(self, env_vars) -> None:
        """A timed-out S3 listing is a 500 and the job is not moved."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        timeout = ReadTimeoutError(endpoint_url="https://s3.eu-central-1.amazonaws.com")

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_owned_job_status", return_value="UPLOADED"):
                with patch("handler.list_uploaded_images", side_effect=timeout):
                    with patch("handler.update_job_item") as mock_update:
                        response = lambda_handler(event, None)

        assert response["statusCode"] == 500
        assert json.loads(response["body"])["message"] == "Failed to list uploaded images."
        mock_update.assert_not_called()

    def test_transport_error_on_update_job_returns_500_without_revert_or_sqs(
        self, env_vars
    ) -> None:
        """An ambiguous conditional write is not reverted, because a revert could undo a winner.

        No SQS message is sent: the job may not have been started by this request.
        """
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        timeout = ReadTimeoutError(endpoint_url="https://dynamodb.eu-central-1.amazonaws.com")

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_owned_job_status", return_value="UPLOADED"):
                with patch("handler.list_uploaded_images", return_value=[f"s3://b/{JOB_ID}/a.png"]):
                    with patch("handler.update_job_item", side_effect=timeout):
                        with patch("handler.revert_job_item") as mock_revert:
                            with patch("handler.trigger_guide_creation") as mock_trigger:
                                response = lambda_handler(event, None)

        assert response["statusCode"] == 500
        assert json.loads(response["body"])["message"] == "Failed to record job."
        mock_revert.assert_not_called()
        mock_trigger.assert_not_called()

    @mock_aws
    def test_race_condition_on_update_job_returns_409_without_sqs_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lost conditional write must not send a second paint message."""
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
            },
        )
        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        with patch.dict(
            os.environ,
            {
                "JOBS_TABLE_NAME": TABLE_NAME,
                "UPLOAD_BUCKET_NAME": BUCKET_NAME,
                "PAINT_QUEUE_URL": queue_url,
            },
        ):
            with patch("handler.update_job_item") as mock_update:
                mock_update.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "Condition check failed",
                        }
                    },
                    "UpdateItem",
                )

                with caplog.at_level(logging.WARNING, logger="handler"):
                    response_result = lambda_handler(event, None)

        assert response_result["statusCode"] == 409
        body = json.loads(response_result["body"])
        assert body == {
            "error": "Conflict",
            "message": "jobId is already in progress or completed",
        }
        messages = sqs.receive_message(QueueUrl=queue_url)
        assert "Messages" not in messages
        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert record.stage == "update_status"
        assert record.jobId == JOB_ID

    @mock_aws
    def test_status_changed_after_read_returns_409_and_keeps_winner_state(self) -> None:
        """A real conditional write losing to a concurrent start leaves the winner's item."""
        event = with_rest_claims(
            {
                "pathParameters": {"jobId": JOB_ID},
                "body": json.dumps({"instruction": "loser instruction"}),
            }
        )
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        s3.put_object(Bucket=BUCKET_NAME, Key=f"uploads/{JOB_ID}/image_0.jpg", Body=b"data")
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}, "userId": {"S": USER_ID}},
        )
        queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]

        def _concurrent_start(job_id: str) -> list:
            """Simulate another request winning the start right after our status read."""
            dynamodb.update_item(
                TableName=TABLE_NAME,
                Key={"jobId": {"S": job_id}},
                UpdateExpression="SET jobStatus = :status",
                ExpressionAttributeValues={":status": {"S": "IN_PROGRESS"}},
            )
            return [f"s3://{BUCKET_NAME}/uploads/{job_id}/image_0.jpg"]

        with patch.dict(
            os.environ,
            {
                "JOBS_TABLE_NAME": TABLE_NAME,
                "UPLOAD_BUCKET_NAME": BUCKET_NAME,
                "PAINT_QUEUE_URL": queue_url,
            },
        ):
            with patch("handler.list_uploaded_images", side_effect=_concurrent_start):
                response = lambda_handler(event, None)

        assert response["statusCode"] == 409
        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "IN_PROGRESS"
        assert "instruction" not in item
        assert "Messages" not in boto3.client("sqs").receive_message(QueueUrl=queue_url)




class TestLambdaHandler:
    """Tests for the full lambda_handler orchestration."""

    @mock_aws
    def test_happy_path_starts_job(self, env_vars) -> None:
        """Full flow: valid jobId, job exists, 1 image, update status."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        prefix = f"uploads/{JOB_ID}/"
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        env_vars["PAINT_QUEUE_URL"] = queue_url

        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["jobId"] == JOB_ID
        assert body["jobStatus"] == "IN_PROGRESS"

    @mock_aws
    def test_invalid_job_id_returns_400(self, env_vars) -> None:
        """Missing jobId should return 400."""
        event = with_rest_claims({"pathParameters": {}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"

    @mock_aws
    def test_job_not_found_returns_404(self, env_vars) -> None:
        """Job not found in DynamoDB should return 404."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NotFound"

    @mock_aws
    def test_job_status_not_uploaded_returns_409(self, env_vars) -> None:
        """Job with non-UPLOADED status should return 409."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "Conflict"

    @mock_aws
    def test_image_count_not_one_returns_422(self, env_vars) -> None:
        """Image count != 1 should return 422."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        prefix = f"uploads/{JOB_ID}/"
        for i in range(2):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "userId": {"S": USER_ID},
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 422
        body = json.loads(result["body"])
        assert body["error"] == "InvalidImageCount"
        assert body["imageCount"] == 2

    @mock_aws
    def test_empty_job_id_returns_400(self, env_vars) -> None:
        """Empty jobId should return 400."""
        event = with_rest_claims({"pathParameters": {"jobId": ""}})
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"


def _arrange_job_in_aws(
    job_status: str = "UPLOADED", owner: str | None = USER_ID, image_count: int = 1
) -> tuple[Any, str]:
    """Create the bucket, jobs table, queue and one job item.

    Returns the DynamoDB client and the queue URL.

    Must run inside an active `mock_aws` context. An `owner` of `None` writes a legacy item
    with no `userId` attribute.

    Args:
        job_status: The `jobStatus` to store on the job item.
        owner: The `userId` to store, or `None` to omit it.
        image_count: How many image objects to upload under `uploads/<JOB_ID>/`.

    Returns:
        A `(dynamodb_client, queue_url)` tuple.
    """
    s3 = boto3.client("s3")
    s3.create_bucket(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
    )
    for index in range(image_count):
        s3.put_object(Bucket=BUCKET_NAME, Key=f"uploads/{JOB_ID}/image_{index}.jpg", Body=b"data")

    dynamodb = boto3.client("dynamodb")
    _create_jobs_table(dynamodb)
    item: dict[str, Any] = {"jobId": {"S": JOB_ID}, "jobStatus": {"S": job_status}}
    if owner is not None:
        item["userId"] = {"S": owner}
    dynamodb.put_item(TableName=TABLE_NAME, Item=item)

    queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]
    return dynamodb, queue_url


def _env_for_queue(queue_url: str) -> dict[str, str]:
    """Return the environment variables the handler needs for a fully mocked run."""
    return {
        "JOBS_TABLE_NAME": TABLE_NAME,
        "UPLOAD_BUCKET_NAME": BUCKET_NAME,
        "PAINT_QUEUE_URL": queue_url,
    }


class TestLambdaHandlerAuthorization:
    """End-to-end ownership and authentication rules for `lambda_handler`."""

    @mock_aws
    def test_jwt_authorizer_shape_starts_job_for_owner(self) -> None:
        """The HTTP API JWT claims shape must start the owner's job like the REST shape."""
        dynamodb, queue_url = _arrange_job_in_aws()
        event = with_jwt_claims({"pathParameters": {"jobId": JOB_ID}})

        with patch.dict(os.environ, _env_for_queue(queue_url)):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "IN_PROGRESS"
        messages = boto3.client("sqs").receive_message(QueueUrl=queue_url)
        assert len(messages["Messages"]) == 1

    @mock_aws
    def test_missing_caller_identity_returns_401_and_leaves_job_untouched(self) -> None:
        """Without an authenticated caller nothing is read, changed, or enqueued."""
        dynamodb, queue_url = _arrange_job_in_aws()
        event = {"pathParameters": {"jobId": JOB_ID}}

        with patch.dict(os.environ, _env_for_queue(queue_url)):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 401
        assert json.loads(result["body"]) == {
            "error": "Unauthorized",
            "message": "Authentication is required.",
        }
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"
        assert "Messages" not in boto3.client("sqs").receive_message(QueueUrl=queue_url)

    @mock_aws
    def test_other_users_job_returns_same_404_as_missing_job_and_stays_uploaded(self) -> None:
        """A non-owner gets the exact response a missing job gets, and the job is untouched."""
        dynamodb, queue_url = _arrange_job_in_aws(owner=USER_ID)
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}}, user_id=OTHER_USER_ID)

        with patch.dict(os.environ, _env_for_queue(queue_url)):
            other_user_result = lambda_handler(event, None)
            missing_job_result = lambda_handler(
                with_rest_claims({"pathParameters": {"jobId": OTHER_JOB_ID}}, OTHER_USER_ID),
                None,
            )

        assert other_user_result["statusCode"] == 404
        assert other_user_result["body"] == missing_job_result["body"]
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in _get_job_item(dynamodb)
        assert "Messages" not in boto3.client("sqs").receive_message(QueueUrl=queue_url)

    @mock_aws
    def test_other_users_in_progress_job_returns_404_not_409(self) -> None:
        """The owner check precedes the status check, so a non-owner learns nothing about status."""
        _arrange_job_in_aws(job_status="IN_PROGRESS", owner=USER_ID)
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}}, user_id=OTHER_USER_ID)

        with patch.dict(os.environ, _env_for_queue(QUEUE_URL)):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 404

    @mock_aws
    def test_legacy_job_without_owner_returns_404_and_is_not_started(self) -> None:
        """A job item created before ownership was recorded cannot be started by anyone."""
        dynamodb, queue_url = _arrange_job_in_aws(owner=None)
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        with patch.dict(os.environ, _env_for_queue(queue_url)):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 404
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"

    @mock_aws
    def test_non_canonical_job_id_returns_400_before_any_aws_read(self) -> None:
        """A malformed jobId is rejected before the owner-scoped DynamoDB read runs."""
        event = with_rest_claims({"pathParameters": {"jobId": f"{JOB_ID}/../other"}})

        with patch("handler.get_owned_job_status") as mock_status:
            result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        mock_status.assert_not_called()

    @mock_aws
    def test_client_caused_422_is_logged_as_warning_not_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A wrong image count is the caller's fault, so it must not raise an ERROR alert."""
        _, queue_url = _arrange_job_in_aws(image_count=2)
        event = with_rest_claims({"pathParameters": {"jobId": JOB_ID}})

        with patch.dict(os.environ, _env_for_queue(queue_url)):
            with caplog.at_level(logging.INFO, logger="handler"):
                result = lambda_handler(event, None)

        assert result["statusCode"] == 422
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        [warning] = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.stage == "validate_images"
        ]
        assert warning.jobId == JOB_ID
