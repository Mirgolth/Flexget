from __future__ import unicode_literals, division, absolute_import

import logging
from math import ceil

from flask import jsonify
from sqlalchemy import Column, Integer, String, ForeignKey, or_, and_, select, update
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound

from flexget import db_schema, plugin
from flexget.api import api, APIResource
from flexget.entry import Entry
from flexget.event import event
from flexget.manager import Session
from flexget.utils import qualities
from flexget.utils.database import quality_requirement_property, with_session
from flexget.utils.imdb import extract_id
from flexget.utils.log import log_once
from flexget.utils.sqlalchemy_utils import table_exists, table_schema

try:
    from flexget.plugins.filter import queue_base
except ImportError:
    raise plugin.DependencyError(issued_by='movie_queue', missing='queue_base',
                                 message='movie_queue requires the queue_base plugin')

log = logging.getLogger('movie_queue')
Base = db_schema.versioned_base('movie_queue', 3)


@event('manager.lock_acquired')
def migrate_imdb_queue(manager):
    """If imdb_queue table is found, migrate the data to movie_queue"""
    session = Session()
    try:
        if table_exists('imdb_queue', session):
            log.info('Migrating imdb_queue items to movie_queue')
            old_table = table_schema('imdb_queue', session)
            for row in session.execute(old_table.select()):
                try:
                    queue_add(imdb_id=row['imdb_id'], quality=row['quality'], session=session)
                except QueueError as e:
                    log.error('Unable to migrate %s from imdb_queue to movie_queue' % row['title'])
            old_table.drop()
            session.commit()
    finally:
        session.close()


@db_schema.upgrade('movie_queue')
def upgrade(ver, session):
    if ver == 0:
        # Translate old qualities into new quality requirements
        movie_table = table_schema('movie_queue', session)
        for row in session.execute(select([movie_table.c.id, movie_table.c.quality])):
            # Webdl quality no longer has dash
            new_qual = row['quality'].replace('web-dl', 'webdl')
            if new_qual.lower() != 'any':
                # Old behavior was to get specified quality or greater, approximate that with new system
                new_qual = ' '.join(qual + '+' for qual in new_qual.split(' '))
            session.execute(update(movie_table, movie_table.c.id == row['id'],
                                   {'quality': new_qual}))
        ver = 1
    if ver == 1:
        # Bad upgrade left some qualities as 'ANY+'
        movie_table = table_schema('movie_queue', session)
        for row in session.execute(select([movie_table.c.id, movie_table.c.quality])):
            if row['quality'].lower() == 'any+':
                session.execute(update(movie_table, movie_table.c.id == row['id'],
                                       {'quality': 'ANY'}))
        ver = 2
    if ver == 2:
        from flexget.utils.imdb import ImdbParser
        # Corrupted movie titles may be in the queue due to imdb layout changes. GitHub #729
        movie_table = table_schema('movie_queue', session)
        queue_base_table = table_schema('queue', session)
        query = select([movie_table.c.id, movie_table.c.imdb_id, queue_base_table.c.title])
        query = query.where(movie_table.c.id == queue_base_table.c.id)
        for row in session.execute(query):
            if row['imdb_id'] and (not row['title'] or row['title'] == 'None' or '\n' in row['title']):
                log.info('Fixing movie_queue title for %s' % row['imdb_id'])
                parser = ImdbParser()
                parser.parse(row['imdb_id'])
                if parser.name:
                    session.execute(update(queue_base_table, queue_base_table.c.id == row['id'],
                                           {'title': parser.name}))
        ver = 3
    return ver


class QueuedMovie(queue_base.QueuedItem, Base):
    __tablename__ = 'movie_queue'
    __mapper_args__ = {'polymorphic_identity': 'movie'}
    id = Column(Integer, ForeignKey('queue.id'), primary_key=True)
    imdb_id = Column(String)
    tmdb_id = Column(Integer)
    quality = Column('quality', String)
    quality_req = quality_requirement_property('quality')

    def to_dict(self):
        return {
            'added': self.added,
            'downloaded': self.downloaded,
            'entry_original_url': self.entry_original_url,
            'entry_title': self.entry_title,
            'entry_url': self.entry_url,
            'id': self.id,
            'imdb_id': self.imdb_id,
            'tmdb_id': self.tmdb_id,
            'quality': self.quality,
            'quality_req': self.quality_req.text,
            'title': self.title,
        }


class MovieQueue(queue_base.FilterQueueBase):
    schema = {
        'oneOf': [
            {'type': 'string', 'enum': ['accept', 'add', 'remove', 'forget']},
            {
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'enum': ['accept', 'add', 'remove', 'forget']},
                    'quality': {'type': 'string', 'format': 'quality_requirements'},
                },
                'required': ['action'],
                'additionalProperties': False
            }
        ]
    }

    def matches(self, task, config, entry):
        if not config:
            return
        if not isinstance(config, dict):
            config = {'action': config}
        # only the accept action is applied in the 'matches' section
        if config.get('action') != 'accept':
            return

        # Tell tmdb_lookup to add lazy lookup fields if not already present
        try:
            plugin.get_plugin_by_name('imdb_lookup').instance.register_lazy_fields(entry)
        except plugin.DependencyError:
            log.debug('imdb_lookup is not available, queue will not work if movie ids are not populated')
        try:
            plugin.get_plugin_by_name('tmdb_lookup').instance.lookup(entry)
        except plugin.DependencyError:
            log.debug('tmdb_lookup is not available, queue will not work if movie ids are not populated')

        conditions = []
        # Check if a movie id is already populated before incurring a lazy lookup
        for lazy in [False, True]:
            if entry.get('imdb_id', eval_lazy=lazy):
                conditions.append(QueuedMovie.imdb_id == entry['imdb_id'])
            if entry.get('tmdb_id', eval_lazy=lazy and not conditions):
                conditions.append(QueuedMovie.tmdb_id == entry['tmdb_id'])
            if conditions:
                break
        if not conditions:
            log_once('IMDB and TMDB lookups failed for %s.' % entry['title'], log, logging.WARN)
            return

        quality = entry.get('quality', qualities.Quality())

        movie = task.session.query(QueuedMovie).filter(QueuedMovie.downloaded == None). \
            filter(or_(*conditions)).first()
        if movie and movie.quality_req.allows(quality):
            return movie

    def on_task_output(self, task, config):
        if not config:
            return
        if not isinstance(config, dict):
            config = {'action': config}
        for entry in task.accepted:
            # Tell tmdb_lookup to add lazy lookup fields if not already present
            try:
                plugin.get_plugin_by_name('tmdb_lookup').instance.lookup(entry)
            except plugin.DependencyError:
                log.debug('tmdb_lookup is not available, queue will not work if movie ids are not populated')
            # Find one or both movie id's for this entry. See if an id is already populated before incurring lazy lookup
            kwargs = {}
            for lazy in [False, True]:
                if entry.get('imdb_id', eval_lazy=lazy):
                    kwargs['imdb_id'] = entry['imdb_id']
                if entry.get('tmdb_id', eval_lazy=lazy):
                    kwargs['tmdb_id'] = entry['tmdb_id']
                if kwargs:
                    break
            if not kwargs:
                log.warning('Could not determine a movie id for %s, it will not be added to queue.' % entry['title'])
                continue

            # Provide movie title if it is already available, to avoid movie_queue doing a lookup
            kwargs['title'] = (entry.get('imdb_name', eval_lazy=False) or
                               entry.get('tmdb_name', eval_lazy=False) or
                               entry.get('movie_name', eval_lazy=False))
            log.debug('movie_queue kwargs: %s' % kwargs)
            try:
                action = config.get('action')
                if action == 'add':
                    # since entries usually have unknown quality we need to ignore that ..
                    if entry.get('quality_req'):
                        kwargs['quality'] = qualities.Requirements(entry['quality_req'])
                    elif entry.get('quality'):
                        kwargs['quality'] = qualities.Requirements(entry['quality'].name)
                    else:
                        kwargs['quality'] = qualities.Requirements(config.get('quality', 'any'))
                    queue_add(**kwargs)
                elif action == 'remove':
                    queue_del(**kwargs)
                elif action == 'forget':
                    queue_forget(**kwargs)
            except QueueError as e:
                # Ignore already in queue errors
                if e.errno != 1:
                    entry.fail('Error adding movie to queue: %s' % e.message)


class QueueError(Exception):
    """Exception raised if there is an error with a queue operation"""

    # TODO: I think message was removed from exception baseclass and is now masked
    # some other custom exception (DependencyError) had to make so tweaks to make it work ..

    def __init__(self, message, errno=0):
        self.message = message
        self.errno = errno


@with_session
def parse_what(what, lookup=True, session=None):
    """
    Determines what information was provided by the search string `what`.
    If `lookup` is true, will fill in other information from tmdb.

    :param what: Can be one of:
      <Movie Title>: Search based on title
      imdb_id=<IMDB id>: search based on imdb id
      tmdb_id=<TMDB id>: search based on tmdb id
    :param bool lookup: Whether missing info should be filled in from tmdb.
    :param session: An existing session that will be used for lookups if provided.
    :rtype: dict
    :return: A dictionary with 'title', 'imdb_id' and 'tmdb_id' keys
    """

    tmdb_lookup = plugin.get_plugin_by_name('api_tmdb').instance.lookup

    result = {'title': None, 'imdb_id': None, 'tmdb_id': None}
    result['imdb_id'] = extract_id(what)
    if not result['imdb_id']:
        if what.startswith('tmdb_id='):
            result['tmdb_id'] = what[8:]
        else:
            result['title'] = what

    if not lookup:
        # If not doing an online lookup we can return here
        return result

    search_entry = Entry(title=result['title'] or '')
    for field in ['imdb_id', 'tmdb_id']:
        if result.get(field):
            search_entry[field] = result[field]
    # Put lazy lookup fields on the search entry
    plugin.get_plugin_by_name('imdb_lookup').instance.register_lazy_fields(search_entry)
    plugin.get_plugin_by_name('tmdb_lookup').instance.lookup(search_entry)

    try:
        # Both ids are optional, but if movie_name was populated at least one of them will be there
        return {'title': search_entry['movie_name'], 'imdb_id': search_entry.get('imdb_id'),
                'tmdb_id': search_entry.get('tmdb_id')}
    except KeyError as e:
        raise QueueError(e.message)


# API functions to edit queue
@with_session
def queue_add(title=None, imdb_id=None, tmdb_id=None, quality=None, session=None):
    """
    Add an item to the queue with the specified quality requirements.

    One or more of `title` `imdb_id` or `tmdb_id` must be specified when calling this function.

    :param title: Title of the movie. (optional)
    :param imdb_id: IMDB id for the movie. (optional)
    :param tmdb_id: TMDB id for the movie. (optional)
    :param quality: A QualityRequirements object defining acceptable qualities.
    :param session: Optional session to use for database updates
    """

    quality = quality or qualities.Requirements('any')

    if not title or not (imdb_id or tmdb_id):
        # We don't have all the info we need to add movie, do a lookup for more info
        result = parse_what(imdb_id or title, session=session)
        title = result['title']
        imdb_id = result['imdb_id']
        tmdb_id = result['tmdb_id']

    # check if the item is already queued
    item = session.query(QueuedMovie).filter(or_(and_(QueuedMovie.imdb_id != None, QueuedMovie.imdb_id == imdb_id),
                                                 and_(QueuedMovie.tmdb_id != None, QueuedMovie.tmdb_id == tmdb_id))). \
        first()
    if not item:
        item = QueuedMovie(title=title, imdb_id=imdb_id, tmdb_id=tmdb_id, quality=quality.text)
        session.add(item)
        log.info('Adding %s to movie queue with quality=%s.' % (title, quality))
        return {'title': title, 'imdb_id': imdb_id, 'tmdb_id': tmdb_id, 'quality': quality}
    else:
        if item.downloaded:
            raise QueueError('ERROR: %s has already been queued and downloaded' % title, errno=1)
        else:
            raise QueueError('ERROR: %s is already in the queue' % title, errno=1)


@with_session
def queue_del(title=None, imdb_id=None, tmdb_id=None, session=None):
    """
    Delete the given item from the queue.

    :param title: Movie title
    :param imdb_id: Imdb id
    :param tmdb_id: Tmdb id
    :param session: Optional session to use, new session used otherwise
    :return: Title of forgotten movie
    :raises QueueError: If queued item could not be found with given arguments
    """
    log.debug('queue_del - title=%s, imdb_id=%s, tmdb_id=%s' % (title, imdb_id, tmdb_id))
    query = session.query(QueuedMovie)
    if imdb_id:
        query = query.filter(QueuedMovie.imdb_id == imdb_id)
    elif tmdb_id:
        query = query.filter(QueuedMovie.tmdb_id == tmdb_id)
    elif title:
        query = query.filter(QueuedMovie.title == title)
    try:
        item = query.one()
        title = item.title
        session.delete(item)
        return title
    except NoResultFound as e:
        raise QueueError('title=%s, imdb_id=%s, tmdb_id=%s not found from queue' % (title, imdb_id, tmdb_id))
    except MultipleResultsFound:
        raise QueueError('title=%s, imdb_id=%s, tmdb_id=%s matches multiple results in queue' %
                         (title, imdb_id, tmdb_id))


@with_session
def queue_forget(title=None, imdb_id=None, tmdb_id=None, session=None):
    """
    Forget movie download  from the queue.

    :param title: Movie title
    :param imdb_id: Imdb id
    :param tmdb_id: Tmdb id
    :param session: Optional session to use, new session used otherwise
    :return: Title of forgotten movie
    :raises QueueError: If queued item could not be found with given arguments
    """
    log.debug('queue_forget - title=%s, imdb_id=%s, tmdb_id=%s' % (title, imdb_id, tmdb_id))
    query = session.query(QueuedMovie)
    if imdb_id:
        query = query.filter(QueuedMovie.imdb_id == imdb_id)
    elif tmdb_id:
        query = query.filter(QueuedMovie.tmdb_id == tmdb_id)
    elif title:
        query = query.filter(QueuedMovie.title == title)
    try:
        item = query.one()
        title = item.title
        if not item.downloaded:
            raise QueueError('%s is not marked as downloaded' % title)
        item.downloaded = None
        return title
    except NoResultFound as e:
        raise QueueError('title=%s, imdb_id=%s, tmdb_id=%s not found from queue' % (title, imdb_id, tmdb_id))


@with_session
def queue_edit(quality, imdb_id=None, tmdb_id=None, session=None):
    """
    :param quality: Change the required quality for a movie in the queue
    :param imdb_id: Imdb id
    :param tmdb_id: Tmdb id
    :param session: Optional session to use, new session used otherwise
    :return: Title of edited item
    :raises QueueError: If queued item could not be found with given arguments
    """
    # check if the item is queued
    try:
        item = session.query(QueuedMovie).filter(QueuedMovie.imdb_id == imdb_id).one()
        item.quality = quality
        return item.title
    except NoResultFound as e:
        raise QueueError('imdb_id=%s, tmdb_id=%s not found from queue' % (imdb_id, tmdb_id))


@with_session
def queue_get(session=None, downloaded=False):
    """
    Get the current movie queue.

    :param session: New session is used it not given
    :param bool downloaded: Whether or not to return only downloaded
    :return: List of QueuedMovie objects (detached from session)
    """
    if not downloaded:
        return session.query(QueuedMovie).filter(QueuedMovie.downloaded == None).all()
    else:
        return session.query(QueuedMovie).filter(QueuedMovie.downloaded != None).all()


@event('plugin.register')
def register_plugin():
    plugin.register(MovieQueue, 'movie_queue', api_ver=2)


movie_queue_api = api.namespace('movie_queue', description='Movie Queue')

movie_queue_schema = {
    'type': 'object',
    'properties': {
        'movies': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'added': {'type': 'string'},
                'downloaded': {'type': 'string'},
                'entry_original_url': {'type': 'string'},
                'entry_title': {'type': 'string'},
                'entry_url': {'type': 'string'},
                'id': {'type': 'integer'},
                'imdb_id': {'type': 'string'},
                'quality': {'type': 'string'},
                'quality_req': {'type': 'string'},
                'title': {'type': 'string'},
                'tmdb_id': {'type': 'string'},
            }
        }
                   },
        'number_of_movies': {'type': 'integer'},
        'total_number_of_pages': {'type': 'integer'},
        'page_number': {'type': 'integer'}

    }
}

movie_queue_schema = api.schema('list_movie_queue', movie_queue_schema)

movie_queue_parser = api.parser()
movie_queue_parser.add_argument('page', type=int, required=False, default=1, help='Page number')
movie_queue_parser.add_argument('max', type=int, required=False, default=50, help='Movies per page')
movie_queue_parser.add_argument('downloaded', type=bool, required=False, default=False, help='Show only downloaded')


@movie_queue_api.route('/')
class MovieQueueListAPI(APIResource):
    @api.response(404, 'Page does not exist')
    @api.response(200, 'Movie queue retrieved successfully', movie_queue_schema)
    @api.doc(parser=movie_queue_parser)
    def get(self, session=None):
        """ List queued movies """
        args = movie_queue_parser.parse_args()
        page = args['page']
        max_results = args['max']
        downloaded = args['downloaded']

        movie_queue = queue_get(session=session, downloaded=downloaded)
        count = len(movie_queue)

        if count == 0:
            return {'success': 'no movies found in queue'}

        pages = int(ceil(count / float(max_results)))

        movie_items = []

        if page > pages:
            return {'error': 'page %s does not exist' % page}, 404

        start = (page - 1) * max_results
        finish = start + max_results
        if finish > count:
            finish = count

        for movie_number in range(start, finish):
            movie_items.append(movie_queue[movie_number])

        return jsonify({
            'movies': [movie.to_dict() for movie in movie_items],
            'number_of_movies': count,
            'page_number': page,
            'total_number_of_pages': pages
        })


movie_add_schema = {
    'type': 'object',
    'properties': {
        'message': {'type': 'string'},
        'title': {'type': 'string'},
        'imdb_id': {'type': 'string'},
        'tmbd_id': {'type': 'string'},
        'quality': {'type': 'string'}
    }
}

movie_queue_add_parser = api.parser()
movie_queue_add_parser.add_argument('title', type=str, required=False, help='Title of movie')
movie_queue_add_parser.add_argument('imdb_id', type=str, required=False, help='IMDB ID of movie')
movie_queue_add_parser.add_argument('tmdb_id', type=str, required=False, help='TMDB ID of movie')
movie_queue_add_parser.add_argument('quality', type=str, required=False, default='any',
                                    help='Quality requirement of movie')


@movie_queue_api.route('/add')
class MovieQueueAddAPI(APIResource):
    @api.response(400, 'Page not found')
    @api.response(200, 'Movie successfully added')
    @api.doc(parser=movie_queue_add_parser)
    def post(self, session=None):
        kwargs = movie_queue_add_parser.parse_args()

        try:
            kwargs['quality'] = qualities.Requirements(kwargs.get('quality'))
        except ValueError as e:
            reply = {
                'status': 'error',
                'message': e.message
            }
            return reply, 400
        kwargs['session'] = session

        try:
            movie = queue_add(**kwargs)
        except QueueError as e:
            reply = {
                'status': 'error',
                'message': e.message
            }
            return reply, 400
        except AttributeError:
            reply = {
                'status': 'error',
                'message': 'Not enough parameters given. Either \"title\", \"imdb_id\" or \"tmdb_id\" are required'}
            return reply, 500

        return jsonify({
            'message': 'Successfully added movie to movie queue',
            'title': movie.get('title'),
            'imdb_id': movie.get('imdb_id'),
            'tmdb_id': movie.get('tmdb_id'),
            'quality': movie.get('quality').text,
        })
