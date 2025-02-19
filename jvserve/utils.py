"""Utility functions for JIVAS."""

from getpass import getpass
from os import getenv

from jac_cloud.core.architype import BulkWrite
from jac_cloud.core.context import SUPER_ROOT_ID
from jac_cloud.jaseci.datasources import Collection
from jac_cloud.jaseci.models import User as BaseUser
from jac_cloud.jaseci.utils import logger
from pymongo.errors import ConnectionFailure, OperationFailure


def create_system_admin() -> str:
    """Create system admin."""
    if not getenv("DATABASE_HOST"):
        raise NotImplementedError("DATABASE_HOST env-var is required for this API!")

    if not (email := getenv("JIVAS_USER")):
        raise NotImplementedError("JIVAS_USER env-var is required for this API!")

    if not (password := getenv("JIVAS_PASSWORD")):
        raise NotImplementedError("JIVAS_PASSWORD env-var is required for this API!")

    if not email:
        trial = 0
        while (email := input("Email: ")) != input("Confirm Email: "):
            if trial > 2:
                raise ValueError("Email don't match! Aborting...")
            print("Email don't match! Please try again.")
            trial += 1

    if not password:
        trial = 0
        while (password := getpass()) != getpass(prompt="Confirm Password: "):
            if trial > 2:
                raise ValueError("Password don't match! Aborting...")
            print("Password don't match! Please try again.")
            trial += 1

    if BaseUser.Collection.find_by_email(email):
        logger.info("User already exists!")
        return "User already exists!"

    user_model = BaseUser.model()
    user_request = user_model.register_type()(
        email=email,
        password=password,
        **user_model.system_admin_default(),
    )

    Collection.apply_indexes()
    with user_model.Collection.get_session() as session, session.start_transaction():
        req_obf: dict = user_request.obfuscate()
        req_obf.update(
            {
                "root_id": SUPER_ROOT_ID,
                "is_activated": True,
                "is_admin": True,
            }
        )

        retry = 0
        max_retry = 1
        while retry <= max_retry:
            try:
                if id := (
                    user_model.Collection.insert_one(req_obf, session=session)
                ).inserted_id:
                    BulkWrite.commit(session)
                    return f"System Admin created with id: {id}"
            except (ConnectionFailure, OperationFailure) as ex:
                if ex.has_error_label("TransientTransactionError"):
                    retry += 1
                    logger.error(
                        "Error executing bulk write! "
                        f"Retrying [{retry}/{max_retry}] ..."
                    )
                    continue
                logger.exception("Error executing bulk write!")
                session.abort_transaction()
                raise
            except Exception:
                logger.exception("Error executing bulk write!")
                session.abort_transaction()
                raise

    raise Exception("Can't process registration. Please try again!")
