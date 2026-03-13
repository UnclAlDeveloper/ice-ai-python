from sqlalchemy import Column, DateTime, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column
from sqlalchemy.orm.base import Mapped

Base = declarative_base()


class OauthTokens(Base):
    __tablename__ = 'oauth_tokens'
    __table_args__ = (
        PrimaryKeyConstraint('provider', name='oauth_tokens_pkey'),
        {'schema': 'ia'}
    )

    provider = mapped_column(String)
    account_id = mapped_column(String)
    access_token = mapped_column(Text)
    access_token_expiry = mapped_column(DateTime(True))
    refresh_token = mapped_column(Text)
    refresh_token_expiry = mapped_column(DateTime(True))
    updated_at = mapped_column(DateTime(True))
