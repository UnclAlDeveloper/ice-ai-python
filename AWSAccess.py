from botocore.exceptions import ClientError

from Settings import get_env_variable

import boto3
import gzip

import dill
import os
import pandas as pd
from pathlib import Path, PurePosixPath
from pydantic import BaseModel
import tempfile
from typing import *


# AWS ACCESS
class AWSAccess(BaseModel):
    """
    AWS interface class that provides methods for interacting with S3, storing and retrieving objects, dataframes, and models.
    """

    bucket_name: Optional[str]
    environment: Optional[str]
    aws_access_key_id: Optional[str]
    aws_account_id: Optional[str]
    aws_secret_access_key: Optional[str]
    aws_session_token: Optional[str]
    aws_region_name: Optional[str]
    specific_path: Optional[str]
    data_path: Optional[str]
    media_path: Optional[str]
    objects_path: Optional[str]
    sns_alert_topic: Optional[str]

    def __init__(
            self,
            bucket_name: str,
            environment: Optional[str] = None,
            sub_directory_name: str = None,
            objects_dir: str = "Objects",
            data_dir: str = "Data",
            media_dir: str = "Media",
            specific_dir: str | None = None,
            aws_account_id: str = get_env_variable("AWS_ACCOUNT_ID"),
            aws_access_key_id: str = get_env_variable("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key: str = get_env_variable("AWS_SECRET_ACCESS_KEY"),
            aws_session_token: str = None,
            aws_region_name: str = get_env_variable("AWS_REGION_NAME"),
            sns_alert_topic: str = get_env_variable("SNS_ALERT_TOPIC"),
    ):
        """
        Initialize the AWS interface with credentials, bucket configuration, and directory paths for organizing stored objects and data.
        """

        data_path = data_dir + ("/" + sub_directory_name if sub_directory_name is not None else "")
        media_path = media_dir + ("/" + sub_directory_name if sub_directory_name is not None else "")
        objects_path = objects_dir + ("/" + sub_directory_name if sub_directory_name is not None else "")
        specific_path = specific_dir + ("/" + sub_directory_name if sub_directory_name is not None else "") if specific_dir is not None else None

        super().__init__(
            aws_access_key_id=aws_access_key_id,
            aws_account_id=aws_account_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            aws_region_name=aws_region_name,
            bucket_name=bucket_name,
            environment=environment,
            data_path=data_path,
            media_path=media_path,
            objects_path=objects_path,
            specific_path=specific_path,
            sns_alert_topic=sns_alert_topic,
        )

    # GET AWS CLIENT
    def _get_aws_client(self, service: str = "s3") -> Any:
        """Get an AWS client for the specified service."""

        return boto3.client(
            service,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
            region_name=self.aws_region_name,
        )

    # GET AWS RESOURCE
    def _get_aws_resource(self, service: str = "s3") -> Any:
        """Get an AWS resource for the specified service."""

        return boto3.resource(
            service,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
            region_name=self.aws_region_name,
        )

    # GET BUCKET
    def get_bucket(self):
        """Get the S3 bucket resource."""

        return self._get_aws_resource("s3").Bucket(self.bucket_name)

    # GET S3 OBJECT
    def get_s3_object(self, key: str):
        """Get an S3 object by key."""

        return self._get_aws_resource("s3").Object(self.bucket_name, key)

    # GET MEDIA
    def get_media(self, name: str, media_type: str):
        """
        Download and return the raw bytes of a media file from the S3 media path.
        """

        return self.get_s3_object(str(PurePosixPath(self.media_path, name).with_suffix("." + media_type))).get()["Body"].read()

    # DOWNLOAD MEDIA TO FILE
    def download_media_to_file(self, name: str, filename: str, media_type: str):
        """
        Download and return the raw bytes of a media file from the S3 media path to a file.
        """

        self.download_file_from_s3(str(PurePosixPath(self.media_path, name).with_suffix("." + media_type)), filename)

    # GET SPECIFIC
    def get_specific(self, specific_name: str):
        """
        Download and return the text or raw bytes of a file stored in the S3 specifics path.
        """

        return self.get_s3_object(self.specifics_path + "/" + specific_name).get()["Body"].read()

    # DOWNLOAD SPECIFIC TO FILE
    def download_specific_to_file(self, specific_name: str, filename: str):
        """
        Download and return the raw bytes of a specific file from the S3 specific path to a file.
        """

        self.download_file_from_s3(self.specifics_path + "/" + specific_name, filename)

    # GET OBJECT
    def get_object(self, object_name: str):
        """
        Download and return the raw bytes of an object from the S3 objects path.
        """

        return self.get_s3_object(self.objects_path + "/" + object_name).get()["Body"].read()

    # GET DATA
    def get_data(self, data_name: str):
        """
        Download and return the raw bytes of a data file from the S3 data path.
        """

        return self.get_s3_object(self.data_path + "/" + data_name).get()["Body"].read()

    # PUT TO S3
    def put_to_s3(self, key: str, body: bytes):
        """Put an object to S3 with the specified key and body."""

        return self.get_bucket().put_object(Key=key, Body=body)

    # UPLOAD TO S3
    def upload_to_s3(self, key: str, filename: Path | str):
        """Upload a file to S3 with the specified key."""

        return self.get_bucket().upload_file(Filename=filename, Key=key)

    # PICKLE AND UPLOAD TO S3
    def _pickle_and_upload_to_s3(self, path: str, name: str, obj: Any, **kwargs):
        """Pickle an object and upload it to S3 as a gzipped file."""

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".pkl.gz", delete=False) as temp:
            temp_name = temp.name
        with gzip.GzipFile(filename=temp_name, mode="wb") as gz:
            dill.dump(obj, gz, **kwargs)

        retval = self.upload_to_s3(path + "/" + name + ".pkl.gz", temp_name)

        os.remove(temp_name)

        return retval

    # SAVE MEDIA
    def save_media(self, name: str, media_type: str, obj: Any):
        """Save a raw media object to S3."""

        return self.put_to_s3(str(PurePosixPath(self.media_path, name).with_suffix("." + media_type)), obj)

    # UPLOAD MEDIA FROM FILE
    def upload_media_from_file(self, name: str, media_type: str, filename: str):
        """Upload a media file to S3."""

        return self.upload_to_s3(str(PurePosixPath(self.media_path, name).with_suffix("." + media_type)), filename)

    # SAVE SPECIFIC
    def save_specific(self, name: str, obj: Any):
        """Save a raw specific object to S3."""

        return self.put_to_s3(str(PurePosixPath(self.specific_path, name)), obj)

    # UPLOAD SPECIFIC FROM FILE
    def upload_specific_from_file(self, name: str, filename: str):
        """Upload a specific file to S3."""

        return self.upload_to_s3(str(PurePosixPath(self.specific_path, name)), filename)

    # SAVE OBJECT
    def save_object(self, object_name: str, obj: Any, **kwargs):
        """
        Serialize an object using dill, compress it with gzip, and upload it to S3 in the objects path.
        """

        return self._pickle_and_upload_to_s3(self.objects_path, object_name, obj, **kwargs)

    # SAVE OBJECT FROM FILE DELEGATE
    def save_object_from_file_delegate(self, object_name: str, delegate: Callable[[str], None], filetype: str = "t"):
        """
        Save an object to S3 by executing a delegate function that writes to a temporary file, then uploading that file.
        """

        with tempfile.NamedTemporaryFile(mode="w" + filetype) as temp:
            delegate(temp.name)
            self.upload_to_s3(self.objects_path + "/" + object_name, temp.name)

    # SAVE OBJECT FROM DIRECTORY DELEGATE
    def save_object_from_directory_delegate(self, object_name: str, delegate: Callable[[str], None]):
        """
        Save an object to S3 by executing a delegate function that writes to a temporary directory, then uploading all files from that directory.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            delegate(temp_dir)
            directory = Path(temp_dir)
            for file_path in directory.iterdir():
                if file_path.is_file():
                    self.upload_to_s3(
                        self.objects_path + "/" + object_name + "/" + file_path.name, directory / file_path.name
                    )

    # SAVE DATAFRAME
    def save_dataframe(self, dataframe_name: str, dataframe: pd.DataFrame, **kwargs):
        """
        Serialize a pandas DataFrame using dill, compress it with gzip, and upload it to S3 in the data path.
        """

        return self._pickle_and_upload_to_s3(self.data_path, dataframe_name, dataframe, **kwargs)

    # UPLOAD OBJECT FROM FILE
    def upload_object_from_file(self, object_name: str, filename: Path):
        """Upload an object to S3 from a file."""

        return self.upload_to_s3(self.objects_path + "/" + object_name, filename)

    # CHECK ON S3
    def _check_on_s3(self, key: str):
        """Check if an object exists on S3."""

        try:
            self._get_aws_client("s3").head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            return False

    # REMOVE FROM S3
    def _remove_from_s3(self, key: str, ignore_if_not_exists: bool):
        """Remove an object from S3."""

        try:
            return self.get_bucket().Object(key).delete()
        except ClientError as e:
            if not ignore_if_not_exists:
                raise e

    # REMOVE OBJECT
    def remove_object(self, object_name: str, ignore_if_not_exists: bool = True):
        """Remove an object from S3 in the objects path."""

        return self._remove_from_s3(self.objects_path + "/" + object_name + ".pkl.gz", ignore_if_not_exists)

    # DOWNLOAD FILE FROM S3
    def download_file_from_s3(self, key: str, filename: Path | str):
        """Download a file from S3."""

        return self.get_bucket().download_file(Key=key, Filename=filename)

    # DOWNLOAD DIRECTORY FROM S3
    def download_directory_from_s3(self, prefix: str, directory: Path | str):
        """Download a directory from S3."""

        continuation_token = None

        while True:
            list_args = {"Bucket": self.bucket_name, "Prefix": prefix}
            if continuation_token:
                list_args["ContinuationToken"] = continuation_token

            result = self._get_aws_client("s3").list_objects_v2(**list_args)

            if "Contents" in result:
                for obj in result["Contents"]:
                    key = obj["Key"]
                    local_file_path = os.path.join(directory, os.path.relpath(key, prefix))
                    self.download_file_from_s3(key, local_file_path)

            if result.get("IsTruncated"):
                continuation_token = result.get("NextContinuationToken")
            else:
                break

    # DOWNLOAD FROM S3 AND UNPICKLE
    def _download_from_s3_and_unpickle(self, path: str, name: str, **kwargs):
        """Download a pickled and gzipped object from S3 and unpickle it."""

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".pkl.gz") as temp:
            self.download_file_from_s3(path + "/" + name + ".pkl.gz", temp.name)
            with gzip.open(temp.name, "rb") as gz_file:
                obj = dill.load(gz_file)

            temp.flush()

            return obj

    # LOAD OBJECT
    def load_object(self, object_name: str, **kwargs):
        """
        Download a gzipped pickled object from S3 in the objects path, decompress it, and deserialize it using dill.
        """

        return self._download_from_s3_and_unpickle(self.objects_path, object_name, **kwargs)

    # LOAD OBJECT FROM FILE DELEGATE
    def load_object_from_file_delegate(
            self, object_name: str, delegate: Callable[[str], None], file_type: str = "t"
    ) -> Any:
        """
        Load an object from S3 by downloading it to a temporary file and executing a delegate function that reads from that file.
        """

        with tempfile.NamedTemporaryFile(mode="w" + file_type) as temp:
            self.download_file_from_s3(self.objects_path + "/" + object_name, temp.name)
            return delegate(temp.name)

    # LOAD OBJECT FROM DIRECTORY DELEGATE
    def load_object_from_directory_delegate(self, model_name: str, delegate: Callable[[str], None]) -> Any:
        """
        Load an object from S3 by downloading a directory structure to a temporary directory and executing a delegate function that reads from it.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            self.download_directory_from_s3(self.objects_path + "/" + model_name, temp_dir)
            return delegate(temp_dir)

    # LOAD DATAFRAME
    def load_dataframe(self, dataframe_name: str, **kwargs):
        """
        Download a gzipped pickled DataFrame from S3 in the data path, decompress it, and deserialize it using dill.
        """

        return self._download_from_s3_and_unpickle(self.data_path, dataframe_name, **kwargs)

    # CHECK FOR OBJECT
    def check_for_object(self, object_name: str, **kwargs):
        """Check if an object exists on S3 in the objects path."""

        return self._check_on_s3(self.objects_path + "/" + object_name + ".pkl.gz")

    # GET PARAMETER VALUE
    def get_parameter_value(self, parameter_name: str, with_decryption: bool = False):
        """Get a parameter value from AWS Systems Manager Parameter Store."""

        return self._get_aws_client("ssm").get_parameter(Name=parameter_name, WithDecryption=with_decryption)[
            "Parameter"
        ]["Value"]

    # GET MEDIA URL
    def get_media_url(self, name: str, media_type: str) -> str:
        """Get the full S3 URL for a media file."""

        key = str(PurePosixPath(self.media_path, name).with_suffix("." + media_type))
        return f"https://{self.bucket_name}.s3.{self.aws_region_name}.amazonaws.com/{key}"

    # GET SPECIFIC URL
    def get_specific_url(self, specific_name: str) -> str:
        """Get the full S3 URL for a specific file."""

        key = self.specific_path + "/" + specific_name
        return f"https://{self.bucket_name}.s3.{self.aws_region_name}.amazonaws.com/{key}"

    # GET OBJECT URL
    def get_object_url(self, object_name: str) -> str:
        """Get the full S3 URL for an object."""

        key = self.objects_path + "/" + object_name + ".pkl.gz"
        return f"https://{self.bucket_name}.s3.{self.aws_region_name}.amazonaws.com/{key}"

    # GET DATA URL
    def get_data_url(self, data_name: str) -> str:
        """Get the full S3 URL for a data file."""

        key = self.data_path + "/" + data_name
        return f"https://{self.bucket_name}.s3.{self.aws_region_name}.amazonaws.com/{key}"

    # SEND NOTIFICATION
    def send_notification(self, message: str, subject: str = None, topic: str = None):
        """Send a notification via AWS SNS."""

        if topic is None:
            topic = self.sns_alert_topic
        if subject is None:
            subject = topic

        topic_arn = f"arn:aws:sns:{self.aws_region_name}:{self.aws_account_id}:{topic}"
        return self._get_aws_client("sns").publish(TopicArn=topic_arn, Message=message, Subject=subject)
