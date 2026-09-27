from alembic import context
from sqlalchemy import create_engine
from app.database import Base, engine
from app import models

config_url = context.config.get_main_option("sqlalchemy.url")
if config_url:
    connectable = create_engine(config_url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()
else:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
