"""
Map the mappingsql store into the wikidata namespace
so we can have special search handling.
"""

import logging

from tiddlyweb.model.tiddler import Tiddler
from tiddlywebplugins.mappingsql import (
        Store as MappingSQLStore, query_dict_to_search_tuple,
        sTiddler, or_)

from sqlalchemy.sql.expression import literal_column

#logging.basicConfig()
#logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)


class Store(MappingSQLStore):
    """
    A subclass of the mappingsql store that adds specific search
    routines for Avox Wiki-Data.
    """

    def _determine_user_access(self):
        """
        MAD uses levels of user access, wiki-data does not.
        """
        return False

    def search(self, search_query=''):
        """
        Search the database for matching tiddlers. Process
        the search query string locally rather than using a provided
        search query, as we will use other query parameters than 'q'.
        """
        full_access = self._determine_user_access()

        query = self.environ.get('tiddlyweb.query', {})

        # Establish pagination if needed
        try:
            slice_index = int(query['index'][0])
            del query['index']
            self.environ['tiddlyweb.mappingsql.index'] = slice_index
        except KeyError:
            slice_index = 0

        # Are we going to be searching branches?
        try:
            branches = query.get('branches', False)
            del query['branches']
        except KeyError:
            pass

        # What version search are we doing?
        try:
            version = int(query.get('v', ['1'])[0])
            del query['v']
        except (KeyError, ValueError):
            version = 1

        # process search type (all, exact, partial)
        if version == 2:
            try:
                type = query.get('type', ['all'])[0]
                del query['type']
            except KeyError:
                pass

        query_string, fields = query_dict_to_search_tuple(
                self.environ.get('tiddlyweb.query', {}))

        query_string = query_string.replace("'", r"\'")

        if version == 2:
            if 'lei' in fields and fields['lei'][0] == 'all':
                # XXX extract to method
                query = self.session.query(getattr(sTiddler, self.id_column))
                query = (query.filter(sTiddler.lei != None)
                        .filter(sTiddler.lei != ''))

                stiddlers = self.run_query(query, branches, full_access,
                        slice_index)

                bag_name = self.environ['tiddlyweb.config']['mappingsql.bag']

                for stiddler in stiddlers:
                    yield Tiddler(unicode(getattr(stiddler, self.id_column)),
                        bag_name)
                return
            if query_string.startswith('""'):
                query_string = '"' + query_string.rstrip('"').lstrip('"') + '"'
            else:
                query_string = query_string.rstrip('"').lstrip('"')
            if not (_is_avid(query_string) or _is_lei(query_string)):
                terms = query_string.split()
                prefix = bound = suffix = ''
                if type == 'all':
                    prefix = '+'
                elif type == 'partial':
                    suffix = '*'
                elif type == 'exact':
                    bound = '"'
                query_string = ' '.join(['%s%s%s'
                    % (prefix, term, suffix) for term in terms])
                if bound:
                    query_string = bound + query_string + bound

        query = self.session.query(getattr(sTiddler, self.id_column))
        have_query = False

        if _is_avid(query_string):
            query = query.filter(getattr(sTiddler, self.id_column)
                    == query_string)
            branches = True
            have_query = True
        elif _is_lei(query_string):
            query = query.filter(sTiddler.lei == query_string)
            branches = True
            have_query = True
        elif query_string:
            if self.environ['tiddlyweb.config'].get(
                    'mappingsql.full_text', False):
                search_fields = ','.join((self.environ['tiddlyweb.config']
                        ['mappingsql.default_search_fields']))
                query = query.filter(
                    "MATCH(%s) AGAINST('%s' in boolean mode)"
                    % (search_fields, query_string))
                if version == 2:
                    expression = literal_column("((MATCH(%s) AGAINST('%s')) + "
                        "(10 * (MATCH(legal_name) AGAINST('\"%s\"' "
                        "in boolean mode))) +"
                        "(1.3 * MATCH(legal_name) AGAINST('%s')))"
                        % (search_fields, query_string, query_string,
                            query_string)).label('relevance')
                    query = query.add_columns(expression).order_by('relevance desc')
            else:
                # XXX: id and modifier fields are not guaranteed to be
                # present. i.e. this code is wrong!
                query = query.filter(or_(
                            sTiddler.id.like('%%%s%%' % query_string),
                            sTiddler.modifier.like('%%%s%%' % query_string)))
            have_query = True

        query, added_query = self.process_fields(query, fields, full_access)
        if added_query:
            have_query = True

        if have_query:
            stiddlers = self.run_query(query, branches, full_access,
                    slice_index)
        else:
            stiddlers = []

        bag_name = self.environ['tiddlyweb.config']['mappingsql.bag']

        for stiddler in stiddlers:
            yield Tiddler(unicode(getattr(stiddler, self.id_column)),
                bag_name)

    def run_query(self, query, branches, full_access, slice_index):
        """
        Run the query, modified by access controls.
        """
        limit = self.environ['tiddlyweb.config'].get(
                'mappingsql.limit', 50)
        if not branches:
            query = query.filter(
                    sTiddler.entity_type != 'SLE').filter(
                            sTiddler.entity_type != 'BRA')
        count = query.count()
        logging.debug('count is: %s', count)
        self.environ['tiddlyweb.mappingsql.count'] = count
        tasters = self.environ[
            'tiddlyweb.config'].get(
                    'mappingsql.tasters', False)
        if tasters and not full_access:
            query = query.filter(sTiddler.taster == 'Y')
        access_count = query.count()
        logging.debug('access_count is: %s', access_count)
        self.environ['tiddlyweb.mappingsql.access_count'] = access_count
        logging.debug('query is: %s', query)
        return query.slice(slice_index, slice_index + limit).all()

    def process_fields(self, query, fields, full_access):
        """
        Add to the query for fields on which we are searching.
        """
        open_fields = self.environ['tiddlyweb.config'].get(
                'mappingsql.open_fields', [])
        have_query = False
        for field in fields:
            if open_fields and not full_access and field not in open_fields:
                continue
            terms = fields[field]
            # TODO: For now we only accept the first term provided
            query = query.filter(getattr(sTiddler, field) == terms[0])
            have_query = True
        return query, have_query


def _is_lei(query_string):
    """
    A (p)lei "a is 20 character code (not 12) with the first 18 characters
    being alphanumeric and the last two being check digits".
    """
    return (query_string.isalnum() and 
            len(query_string) == 20 and
            query_string[-2:].isdigit())


def _is_avid(query_string):
    """
    An avid is defined as a string of digits between
    7 and 10 characters long. Otherwise we think of it as
    a company name.
    """
    return query_string.isdigit() and (7 <= len(query_string) <= 10)
