import os
from abc import ABC, abstractmethod
import logging


FILE_INTERFACE = os.environ.get("JIVAS_FILE_INTERFACE", "local")


class FileInterface(ABC):
    __root_dir: str = ""
    LOGGER: logging.Logger = logging.getLogger(__name__)

    @abstractmethod
    def get_file(self, filename: str) -> bytes | None:
        pass

    @abstractmethod
    def save_file(self, filename: str, content: bytes) -> bool:
        pass

    @abstractmethod
    def delete_file(self, filename: str) -> bool:
        pass

    @abstractmethod
    def get_file_url(self, filename: str) -> str | None:
        pass


class LocalFileInterface(FileInterface):
    def __init__(self, files_root: str = "") -> None:
        self.__root_dir = files_root

    def get_file(self, filename: str) -> bytes | None:
        file_path = os.path.join(self.__root_dir, filename)
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                return f.read()
        return None

    def save_file(self, filename: str, content: bytes) -> bool:
        file_path = os.path.join(self.__root_dir, filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(content)
        return True

    def delete_file(self, filename: str) -> bool:
        file_path = os.path.join(self.__root_dir, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        return False

    def get_file_url(self, filename: str) -> str | None:
        file_path = os.path.join(self.__root_dir, filename)
        if os.path.exists(file_path):
            return f"{os.environ.get('JIVAS_FILES_URL','http://localhost:9000/files')}/{filename}"
        return None


class S3FileInterface(FileInterface):
    def __init__(
        self,
        bucket_name: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        endpoint_url: str | None = None,
        files_root: str = ".files",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            endpoint_url=endpoint_url,
            config=Config(signature_version="v4"),
        )
        self.bucket_name = bucket_name
        self.__root_dir = files_root

        # Check for missing AWS credentials
        if not aws_access_key_id or not aws_secret_access_key or not region_name:
            FileInterface.LOGGER.warn(
                "Missing AWS credentials - S3 operations may fail"
            )

    def get_file(self, filename: str) -> bytes | None:
        try:
            file_key = os.path.join(self.__root_dir, filename)
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=file_key)
            return response["Body"].read()
        except Exception:
            return None

    def save_file(self, filename: str, content: bytes) -> bool:
        try:
            file_key = os.path.join(self.__root_dir, filename)
            self.s3_client.put_object(
                Bucket=self.bucket_name, Key=file_key, Body=content
            )
            return True
        except Exception:
            return False

    def delete_file(self, filename: str) -> bool:
        try:
            file_key = os.path.join(self.__root_dir, filename)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=file_key)
            return True
        except Exception:
            return False

    def get_file_url(self, filename: str) -> str | None:
        try:
            file_key = os.path.join(self.__root_dir, filename)
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": file_key},
                ExpiresIn=3600,
            )
            return url
        except Exception:
            return None


if FILE_INTERFACE == "s3":
    file_interface = S3FileInterface(
        bucket_name=os.environ.get("JIVAS_S3_BUCKET_NAME", ""),
        region_name=os.environ.get("JIVAS_S3_REGION_NAME", "us-east-1"),
        aws_access_key_id=os.environ.get("JIVAS_S3_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("JIVAS_S3_SECRET_ACCESS_KEY", ""),
        endpoint_url=os.environ.get("JIVAS_S3_ENDPOINT_URL", None),
        files_root=os.environ.get("JIVAS_FILES_ROOT_PATH", ".files"),
    )
else:
    file_interface = LocalFileInterface(
        files_root=os.environ.get("JIVAS_FILES_ROOT_PATH", ".files")
    )
