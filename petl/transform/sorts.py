from __future__ import absolute_import, print_function, division, \
    unicode_literals


import heapq
from tempfile import NamedTemporaryFile
import itertools
import logging
from collections import namedtuple
import operator
from petl.compat import pickle, next


from petl.comparison import comparable_itemgetter
from petl.util.base import Table, asindices


logger = logging.getLogger(__name__)
warning = logger.warning
info = logger.info
debug = logger.debug


def sort(table, key=None, reverse=False, buffersize=None, tempdir=None,
         cache=True):
    """Sort the table. Field names or indices (from zero) can be used to specify
    the key. E.g.::

        >>> from petl import sort, look
        >>> table1 = [['foo', 'bar'],
        ...           ['C', 2],
        ...           ['A', 9],
        ...           ['A', 6],
        ...           ['F', 1],
        ...           ['D', 10]]
        >>> table2 = sort(table1, 'foo')
        >>> look(table2)
        +-------+-------+
        | 'foo' | 'bar' |
        +=======+=======+
        | 'A'   |     9 |
        +-------+-------+
        | 'A'   |     6 |
        +-------+-------+
        | 'C'   |     2 |
        +-------+-------+
        | 'D'   |    10 |
        +-------+-------+
        | 'F'   |     1 |
        +-------+-------+

        >>> # sorting by compound key is supported
        ... table3 = sort(table1, key=['foo', 'bar'])
        >>> look(table3)
        +-------+-------+
        | 'foo' | 'bar' |
        +=======+=======+
        | 'A'   |     6 |
        +-------+-------+
        | 'A'   |     9 |
        +-------+-------+
        | 'C'   |     2 |
        +-------+-------+
        | 'D'   |    10 |
        +-------+-------+
        | 'F'   |     1 |
        +-------+-------+

        >>> # if no key is specified, the default is a lexical sort
        ... table4 = sort(table1)
        >>> look(table4)
        +-------+-------+
        | 'foo' | 'bar' |
        +=======+=======+
        | 'A'   |     6 |
        +-------+-------+
        | 'A'   |     9 |
        +-------+-------+
        | 'C'   |     2 |
        +-------+-------+
        | 'D'   |    10 |
        +-------+-------+
        | 'F'   |     1 |
        +-------+-------+

    The `buffersize` argument should be an `int` or `None`.

    If the number of rows in the table is less than `buffersize`, the table
    will be sorted in memory. Otherwise, the table is sorted in chunks of
    no more than `buffersize` rows, each chunk is written to a temporary file,
    and then a merge sort is performed on the temporary files.

    If `buffersize` is `None`, the value of
    `petl.transform.sorts.defaultbuffersize` will be used. By default this is
    set to 100000 rows, but can be changed, e.g.::

        >>> import petl.transform.sorts
        >>> petl.transform.sorts.defaultbuffersize = 500000

    If `petl.transform.sorts.defaultbuffersize` is set to `None`, this forces
    all sorting to be done entirely in memory.

    By default the results of the sort will be cached, and so a second pass over
    the sorted table will yield rows from the cache and will not repeat the
    sort operation. To turn off caching, set the `cache` argument to `False`.

    """

    return SortView(table, key=key, reverse=reverse, buffersize=buffersize,
                    tempdir=tempdir, cache=cache)


Table.sort = sort


def iterchunk(f):
    # reopen so iterators from file cache are independent
    with open(f.name, 'rb') as f:
        try:
            while True:
                yield pickle.load(f)
        except EOFError:
            pass


_Keyed = namedtuple('Keyed', ['key', 'obj'])


def _heapqmergesorted(key=None, *iterables):
    """Return a single iterator over the given iterables, sorted by the
    given `key` function, assuming the input iterables are already sorted by
    the same function. (I.e., the merge part of a general merge sort.) Uses
    :func:`heapq.merge` for the underlying implementation."""

    if key is None:
        keyed_iterables = iterables
        for element in heapq.merge(*keyed_iterables):
            yield element
    else:
        keyed_iterables = [(_Keyed(key(obj), obj) for obj in iterable)
                           for iterable in iterables]
        for element in heapq.merge(*keyed_iterables):
            yield element.obj


def _shortlistmergesorted(key=None, reverse=False, *iterables):
    """Return a single iterator over the given iterables, sorted by the
    given `key` function, assuming the input iterables are already sorted by
    the same function. (I.e., the merge part of a general merge sort.) Uses
    :func:`min` (or :func:`max` if ``reverse=True``) for the underlying
    implementation."""

    if reverse:
        op = max
    else:
        op = min
    if key is not None:
        opkwargs = {'key': key}
    else:
        opkwargs = dict()
    # populate initial shortlist
    # (remember some iterables might be empty)
    iterators = list()
    shortlist = list()
    for iterable in iterables:
        it = iter(iterable)
        try:
            first = next(it)
            iterators.append(it)
            shortlist.append(first)
        except StopIteration:
            pass
    # do the mergesort
    while iterators:
        nxt = op(shortlist, **opkwargs)
        yield nxt
        nextidx = shortlist.index(nxt)
        try:
            shortlist[nextidx] = next(iterators[nextidx])
        except StopIteration:
            del shortlist[nextidx]
            del iterators[nextidx]


def _mergesorted(key=None, reverse=False, *iterables):
    # N.B., I've used heapq for normal merge sort and shortlist merge sort for
    # reverse merge sort because I've assumed that heapq.merge is faster and
    # so is preferable but it doesn't support reverse sorting so the shortlist
    # merge sort has to be used for reverse sorting. Some casual profiling
    # suggests there isn't much between the two in terms of speed, but might be
    # worth profiling more carefully

    if reverse:
        return _shortlistmergesorted(key, True, *iterables)
    else:
        return _heapqmergesorted(key, *iterables)


defaultbuffersize = 100000


class SortView(Table):
    def __init__(self, source, key=None, reverse=False, buffersize=None,
                 tempdir=None, cache=True):
        self.source = source
        self.key = key
        self.reverse = reverse
        if buffersize is None:
            self.buffersize = defaultbuffersize
        else:
            self.buffersize = buffersize
        self.tempdir = tempdir
        self.cache = cache
        self._fldcache = None
        self._memcache = None
        self._filecache = None
        self._getkey = None

    def clearcache(self):
        self._clearcache()

    def _clearcache(self):
        self._fldcache = None
        self._memcache = None
        self._filecache = None
        self._getkey = None

    def __iter__(self):
        source = self.source
        key = self.key
        reverse = self.reverse
        if self.cache and self._memcache is not None:
            return self._iterfrommemcache()
        elif self.cache and self._filecache is not None:
            return self._iterfromfilecache()
        else:
            return self._iternocache(source, key, reverse)

    def _iterfrommemcache(self):
        debug('iterate from mem cache')
        yield tuple(self._fldcache)
        for row in self._memcache:
            yield tuple(row)

    def _iterfromfilecache(self):
        debug('iterate from file cache: %r', [f for f in self._filecache])
        yield tuple(self._fldcache)
        chunkiters = [iterchunk(f) for f in self._filecache]
        for row in _mergesorted(self._getkey, self.reverse, *chunkiters):
            yield tuple(row)

    def _iternocache(self, source, key, reverse):
        debug('iterate without cache')
        self._clearcache()
        it = iter(source)

        flds = next(it)
        yield tuple(flds)

        if key is not None:
            # convert field selection into field indices
            indices = asindices(flds, key)
        else:
            indices = range(len(flds))
        # now use field indices to construct a _getkey function
        # N.B., this will probably raise an exception on short rows
        getkey = comparable_itemgetter(*indices)

        # TODO support native comparison

        # initialise the first chunk
        rows = list(itertools.islice(it, 0, self.buffersize))
        # print(repr(getkey))
        # print(rows)
        # for row in rows:
        # print(row, getkey(row))
        rows.sort(key=getkey, reverse=reverse)

        # have we exhausted the source iterator?
        if self.buffersize is None or len(rows) < self.buffersize:

            if self.cache:
                debug('caching mem')
                self._fldcache = flds
                self._memcache = rows
                # actually not needed to iterate from memcache
                self._getkey = getkey

            for row in rows:
                yield tuple(row)

        else:

            chunkfiles = []

            while rows:

                # dump the chunk
                f = NamedTemporaryFile(dir=self.tempdir)
                for row in rows:
                    pickle.dump(row, f, protocol=-1)
                f.flush()
                # N.B., do not close the file! Closing will delete
                # the file, and we might want to keep it around
                # if it can be cached. We'll let garbage collection
                # deal with this, i.e., when no references to the
                # chunk files exist any more, garbage collection
                # should be an implicit close, which will cause file
                # deletion.
                chunkfiles.append(f)

                # grab the next chunk
                rows = list(itertools.islice(it, 0, self.buffersize))
                rows.sort(key=getkey, reverse=reverse)

            if self.cache:
                debug('caching files %r', chunkfiles)
                self._fldcache = flds
                self._filecache = chunkfiles
                self._getkey = getkey

            chunkiters = [iterchunk(f) for f in chunkfiles]
            for row in _mergesorted(getkey, reverse, *chunkiters):
                yield tuple(row)


def mergesort(*tables, **kwargs):
    """Combine multiple input tables into one sorted output table. E.g.::

        >>> from petl import mergesort, look
        >>> table1 = [['foo', 'bar'],
        ...           ['A', 9],
        ...           ['C', 2],
        ...           ['D', 10],
        ...           ['A', 6],
        ...           ['F', 1]]
        >>> table2 = [['foo', 'bar'],
        ...           ['B', 3],
        ...           ['D', 10],
        ...           ['A', 10],
        ...           ['F', 4]]
        >>> table3 = mergesort(table1, table2, key='foo')
        >>> look(table3)
        +-------+-------+
        | 'foo' | 'bar' |
        +=======+=======+
        | 'A'   |     9 |
        +-------+-------+
        | 'A'   |     6 |
        +-------+-------+
        | 'A'   |    10 |
        +-------+-------+
        | 'B'   |     3 |
        +-------+-------+
        | 'C'   |     2 |
        +-------+-------+

    If the input tables are already sorted by the given key, give
    ``presorted=True`` as a keyword argument.

    This function is equivalent to concatenating the input tables using
    :func:`cat` then sorting, however this function will typically be more
    efficient, especially if the input tables are presorted.

    Keyword arguments:

    key : string or tuple of strings, optional
        Field name or tuple of fields to sort by (defaults to `None` lexical
        sort)
    reverse : bool, optional
        `True` if sort in reverse (descending) order (defaults to `False`)
    presorted : bool, optional
        `True` if inputs are already sorted by the given key (defaults to
        `False`)
    missing : object
        Value to fill with when input tables have different fields (defaults to
        `None`)
    header : sequence of strings, optional
        Specify a fixed header for the output table
    buffersize : int, optional
        Limit the number of rows in memory per input table when inputs are not
        presorted

    """

    return MergeSortView(tables, **kwargs)


Table.mergesort = mergesort


class MergeSortView(Table):
    def __init__(self, tables, key=None, reverse=False, presorted=False,
                 missing=None, header=None, buffersize=None, tempdir=None,
                 cache=True):
        self.key = key
        if presorted:
            self.tables = tables
        else:
            self.tables = [sort(t, key=key, reverse=reverse,
                                buffersize=buffersize, tempdir=tempdir,
                                cache=cache)
                           for t in tables]
        self.missing = missing
        self.header = header
        self.reverse = reverse

    def __iter__(self):
        return itermergesort(self.tables, self.key, self.header, self.missing,
                             self.reverse)


def itermergesort(sources, key, header, missing, reverse):
    # first need to standardise headers of all input tables
    # borrow this from itercat - TODO remove code smells

    its = [iter(t) for t in sources]
    source_flds_lists = [next(it) for it in its]

    if header is None:
        # determine output fields by gathering all fields found in the sources
        outflds = list()
        for flds in source_flds_lists:
            for f in flds:
                if f not in outflds:
                    # add any new fields as we find them
                    outflds.append(f)
    else:
        # predetermined output fields
        outflds = header
    yield tuple(outflds)

    def _standardisedata(it, flds, ofs):
        # now construct and yield the data rows
        for _row in it:
            try:
                # should be quickest to do this way
                yield tuple(_row[flds.index(fo)] if fo in flds else missing
                            for fo in ofs)
            except IndexError:
                # handle short rows
                outrow = [missing] * len(ofs)
                for i, fi in enumerate(flds):
                    try:
                        outrow[ofs.index(fi)] = _row[i]
                    except IndexError:
                        pass  # be relaxed about short rows
                yield tuple(outrow)

    # wrap all iterators to standardise fields
    sits = [_standardisedata(it, flds, outflds)
            for flds, it in zip(source_flds_lists, its)]

    # now determine key function
    getkey = None
    if key is not None:
        # convert field selection into field indices
        indices = asindices(outflds, key)
        # now use field indices to construct a _getkey function
        # N.B., this will probably raise an exception on short rows
        getkey = comparable_itemgetter(*indices)

    # OK, do the merge sort
    for row in _shortlistmergesorted(getkey, reverse, *sits):
        yield row


def issorted(table, key=None, reverse=False, strict=False):
    """
    Return True if the table is ordered (i.e., sorted) by the given key. E.g.::

        >>> from petl import issorted, look
        >>> look(table)
        +-------+-------+-------+
        | 'foo' | 'bar' | 'baz' |
        +=======+=======+=======+
        | 'a'   | 1     | True  |
        +-------+-------+-------+
        | 'b'   | 3     | True  |
        +-------+-------+-------+
        | 'b'   | 2     |       |
        +-------+-------+-------+

        >>> issorted(table, key='foo')
        True
        >>> issorted(table, key='foo', strict=True)
        False
        >>> issorted(table, key='foo', reverse=True)
        False

    """

    # determine the operator to use when comparing rows
    if reverse and strict:
        op = operator.lt
    elif reverse and not strict:
        op = operator.le
    elif strict:
        op = operator.gt
    else:
        op = operator.ge

    it = iter(table)
    fnms = [str(f) for f in next(it)]
    if key is None:
        prev = next(it)
        for curr in it:
            if not op(curr, prev):
                return False
            prev = curr
    else:
        getkey = comparable_itemgetter(*asindices(fnms, key))
        prev = next(it)
        prevkey = getkey(prev)
        for curr in it:
            currkey = getkey(curr)
            if not op(currkey, prevkey):
                return False
            prevkey = currkey
    return True


Table.issorted = issorted
