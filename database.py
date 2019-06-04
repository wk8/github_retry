from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


def init_db(db_file_name='db.sqlite'):
    engine = create_engine('sqlite:///%s' % (db_file_name, ))
    db_session = scoped_session(sessionmaker(bind=engine))

    # import all modules here that might define models so that
    # they will be registered properly on the metadata.  Otherwise
    # you will have to import them first before calling init_db()
    import models
    Base.metadata.create_all(bind=engine)

    return db_session
