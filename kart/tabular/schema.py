import functools
import re
import uuid
from collections import namedtuple

import pygit2

from kart.geometry import Geometry
from kart.serialise_util import (
    hexhash,
    json_pack,
    json_unpack,
    msg_pack,
    msg_unpack,
    sha256,
)


class Legend:
    """
    A legend is like a table-header that describes how a row is read. Legends are immutable.
    A row is an array of values - eg ["Null Island", POINT(0, 0), "Imaginary points of interest"]
    To read this, we need to look at the legend, which is an array of UUIDs, like:
    ["1144c97-73a6-78d38ab68ed", "2f1551c-a094-c1a3678e317", "2feac23-08af-73e61dbced9"],
    The two arrays can be zipped together to make a dict of (column-id, value) pairs, ie:
    {
        "1144c97-73a6-78d38ab68ed": "Null Island",
        "2f1551c-a094-c1a3678e317": POINT(0, 0),
        "2feac23-08af-73e61dbced9": "Imaginary points of interest"
    }
    Then the schema can be consulted to find the current user-visible names and ordering of those columns.

    In practise, rows are split into two parts when written - the primary key columns are embedded in the file path,
    and the remaining columns are written to the file contents. For this reason, we receive rows as two arrays -
    primary-key values and non-primary-key values - and so a legend contains two arrays of UUIDs for making sense of
    both parts.
    """

    def __init__(self, pk_columns, non_pk_columns):
        """
        Create a new legend.
            pk_columns - a list of column IDs for primary key columns.
            non_pk_columns - a list of column IDs for non primary key columns.
        """
        self._pk_columns = tuple(pk_columns)
        self._non_pk_columns = tuple(non_pk_columns)

    @property
    def pk_columns(self):
        return self._pk_columns

    @property
    def non_pk_columns(self):
        return self._non_pk_columns

    @classmethod
    def loads(cls, data):
        """Load a legend from a bytestring"""
        pk_columns, non_pk_columns = msg_unpack(data)
        return cls(pk_columns, non_pk_columns)

    def dumps(self):
        """Writes this legend to a bytestring."""
        return msg_pack((self.pk_columns, self.non_pk_columns))

    def value_tuples_to_raw_dict(self, pk_values, non_pk_values):
        """
        Given all the values, zips them into a dict with their column IDs.
        This dict is called the "raw" dict since it uses internal column IDs, not the user-visible column names.
        Legends only deal with raw dicts - for user-visible properties, see Schema.
        """
        assert len(pk_values) == len(self.pk_columns)
        assert len(non_pk_values) == len(self.non_pk_columns)
        raw_feature_dict = {}
        for column, value in zip(self.pk_columns, pk_values):
            raw_feature_dict[column] = value
        for column, value in zip(self.non_pk_columns, non_pk_values):
            raw_feature_dict[column] = value
        return raw_feature_dict

    def raw_dict_to_value_tuples(self, raw_feature_dict):
        """Inverse of value_tuples_to_raw_dict."""
        pk_values = tuple(raw_feature_dict[column] for column in self.pk_columns)
        non_pk_values = tuple(
            raw_feature_dict[column] for column in self.non_pk_columns
        )
        return pk_values, non_pk_values

    def __eq__(self, other):
        if not isinstance(other, Legend):
            return False
        return (
            self.pk_columns == other.pk_columns
            and self.non_pk_columns == other.non_pk_columns
        )

    def __hash__(self):
        return hash((self.pk_columns, self.non_pk_columns))

    def hexhash(self):
        """Like __hash__ but with platform-independent, 160-bit hex strings."""
        return hexhash(self.dumps())


def pk_index_ordering(column):
    """Returns primary key columns first, in pk_index order, then other columns."""
    if column.pk_index is not None:
        return column.pk_index
    else:
        # Non primary-key columns after primary key columns.
        return float("inf")


ALL_DATA_TYPES = {
    "boolean",
    "blob",
    "date",
    "float",
    "geometry",
    "integer",
    "interval",
    "numeric",
    "text",
    "time",
    "timestamp",
}


class ColumnSchema(
    # namedtuple for Immutability
    namedtuple(
        "ColumnSchema", ("id", "name", "data_type", "pk_index", "extra_type_info")
    )
):
    """
    The schema for a single column. A column has a unique ID that is constant for the columns lifetime
    - even if the column is moved or renamed - and it has a name, a datatype, info about if this
    column is one of the primary keys, and potentially extra info about the specific datatype.
    """

    @staticmethod
    def new_id():
        return str(uuid.uuid4())

    @staticmethod
    def deterministic_id(*data):
        """*data (any types) -> str(UUID). Deterministic, irreversible."""
        bytes16 = sha256(*data).digest()[:16]
        return str(uuid.UUID(bytes=bytes16))

    def __new__(cls, id, name, data_type, pk_index, **extra_type_info):
        assert data_type in ALL_DATA_TYPES, data_type
        return super().__new__(cls, id, name, data_type, pk_index, extra_type_info)

    @classmethod
    def from_dict(cls, d):
        d = d.copy()
        id_ = d.pop("id")
        name = d.pop("name")
        data_type = d.pop("dataType")
        pk_index = d.pop("primaryKeyIndex", None)
        extra_type_info = dict((k, v) for k, v in d.items() if v is not None)
        return cls(id_, name, data_type, pk_index, **extra_type_info)

    def to_dict(self):
        result = {"id": self.id, "name": self.name, "dataType": self.data_type}
        if self.pk_index is not None:
            result["primaryKeyIndex"] = self.pk_index
        for key, value in self.extra_type_info.items():
            if value is not None:
                result[key] = value
        return result

    def __eq__(self, other):
        if not isinstance(other, ColumnSchema):
            return False
        return (
            self.id == other.id
            and self.name == other.name
            and self.data_type == other.data_type
            and self.pk_index == other.pk_index
            and self.extra_type_info == other.extra_type_info
        )

    def __hash__(self):
        return hash(
            (
                self.id,
                self.name,
                self.data_type,
                self.pk_index,
                frozenset(self.extra_type_info.items()),
            )
        )

    @classmethod
    def _normalise_dict(cls, col_dict):
        for key in list(col_dict.keys()):
            if key not in ("id", "name", "dataType") and col_dict[key] is None:
                del col_dict[key]


class Schema:
    """A schema is just an immutable ordered list of ColumnSchemas."""

    def __init__(self, columns):
        """Create a new schema from a list of ColumnSchemas."""
        self._columns = tuple(columns)
        # Creating a legend validates the primaryKeyIndex field.
        self._legend = self._to_legend()
        self._pk_columns = tuple(
            c for c in sorted(columns, key=pk_index_ordering) if c.pk_index is not None
        )
        self._hash = hash(self._columns)

    @property
    def columns(self):
        return self._columns

    @property
    def column_names(self):
        return [c.name for c in self._columns]

    @property
    def legend(self):
        return self._legend

    @property
    def pk_columns(self):
        return self._pk_columns

    @property
    @functools.lru_cache(maxsize=1)
    def non_pk_columns(self):
        return tuple(c for c in self.columns if c.pk_index is None)

    @property
    @functools.lru_cache(maxsize=1)
    def geometry_columns(self):
        return tuple(c for c in self.columns if c.data_type == "geometry")

    @property
    def has_geometry(self):
        return bool(self.geometry_columns)

    def __getitem__(self, i):
        """Return the _i_th ColumnSchema, or, the ColumnSchema with the given ID."""
        if isinstance(i, str):
            try:
                return next(c for c in self.columns if c.id == i)
            except StopIteration:
                raise KeyError(f"No such column: {i}")

        return self._columns[i]

    def __contains__(self, id):
        return any(c.id == id for c in self.columns)

    @classmethod
    def from_column_dicts(cls, column_dicts):
        columns = [ColumnSchema.from_dict(d) for d in column_dicts]
        return cls(columns)

    @classmethod
    def normalise_column_dicts(cls, column_dicts):
        return Schema.from_column_dicts(column_dicts).to_column_dicts()

    @classmethod
    def loads(cls, data):
        """Load a schema from a bytestring"""
        return cls.from_column_dicts(json_unpack(data))

    def to_column_dicts(self):
        return [c.to_dict() for c in self.columns]

    def dumps(self):
        """Writes this schema to a bytestring."""
        return json_pack(self.to_column_dicts())

    def __str__(self):
        cols = ",\n".join(str(c) for c in self.columns)
        return f"Schema([{cols}])"

    def __repr__(self):
        cols = ",\n".join(repr(c) for c in self.columns)
        return f"Schema([{cols}])"

    def feature_from_raw_dict(self, raw_dict):
        """
        Takes a "raw" feature dict - values keyed by column ID.
        Returns a dict of values keyed by column name.
        """
        return {c.name: raw_dict.get(c.id, None) for c in self.columns}

    def feature_to_raw_dict(self, feature):
        """
        Takes a feature - either a dict of values keyed by column name,
        or a list / tuple of values in schema order.
        Returns a "raw" feature dict - values keyed by column ID.
        """
        raw_dict = {}
        if isinstance(feature, dict) or hasattr(feature, "keys"):
            # Feature values are keyed by column name - find them by key.
            # This also works for DB rows that are subscriptable.
            for column in self.columns:
                raw_dict[column.id] = feature[column.name]
        else:
            # Feature values are not keyed, but should be in order, one per column.
            assert len(feature) == len(self.columns)
            for column, value in zip(self.columns, feature):
                raw_dict[column.id] = value
        return raw_dict

    def encode_feature(self, feature, without_pk=False):
        """
        Given a feature, encode it in binary using this schema.
        If without_pk is True, the resulting bytes don't depend on the feature's pk values.
        """
        raw_dict = self.feature_to_raw_dict(feature)
        pk_values, non_pk_values = self.legend.raw_dict_to_value_tuples(raw_dict)
        legend_hash = self.legend.hexhash()
        data = (
            [legend_hash, non_pk_values]
            if without_pk
            else [legend_hash, pk_values, non_pk_values]
        )
        return msg_pack(data)

    def hash_feature(self, feature, without_pk=False):
        """
        Given a feature, git-hash it using this schema.
        If without_pk is True, the resulting hash doesn't depend on the feature's pk values.
        """
        data = self.encode_feature(feature, without_pk=without_pk)
        return pygit2.hash(data).hex

    def _to_legend(self):
        pk_column_ids = []
        non_pk_column_ids = []
        for i, column in enumerate(sorted(self.columns, key=pk_index_ordering)):
            if column.pk_index is not None:
                if i != column.pk_index:
                    raise ValueError(
                        f"Expected contiguous primaryKeyIndex {i} but only found {column.pk_index}"
                    )
                pk_column_ids.append(column.id)
            else:
                non_pk_column_ids.append(column.id)
        return Legend(pk_column_ids, non_pk_column_ids)

    def __eq__(self, other):
        if not isinstance(other, Schema):
            return False
        return self.columns == other.columns

    def __hash__(self):
        return self._hash

    def is_pk_compatible(self, other):
        """
        Does a schema change from self -> other mean that every feature needs a new path?
        Only if the primary key columns have changed.
        """
        return self.legend.pk_columns == other.legend.pk_columns

    def sanitise_pks(self, pk_values):
        """
        Fixes two common problems with pk_values, particularly if pk_values were provided by the user
        as text and so had to be parsed from text:
        1. pk_values should be a list / tuple, with one value per primary key column.
           (There is hardly ever >1 primary key column, but as this is what our model supports, we need a list.)
        2. integer columns need int values, so if the values were supplied as text, we need to cast to int here.
        """
        if isinstance(pk_values, tuple):
            pk_values = list(pk_values)
        elif not isinstance(pk_values, list):
            pk_values = [pk_values]
        for i, (value, column) in enumerate(zip(pk_values, self.pk_columns)):
            if column.data_type == "integer" and isinstance(value, str):
                pk_values[i] = int(pk_values[i])
            elif column.data_type == "float" and isinstance(value, str):
                # not sure why you'd use floats as a PK value, but we came across it at least once
                pk_values[i] = float(pk_values[i])
        return tuple(pk_values)

    def align_to_self(self, new_schema, roundtrip_ctx=None):
        """
        Returns a new schema the same as the one given, except that some column IDs might be changed to match those in
        self. Column IDs are copied from self onto the resulting schema if the columns in the resulting schema
        are the "same" as columns in the previous schema. Uses a heuristic - columns are the same if they have the
        same name + type (handles reordering), or, if they have the same position and type (handles renames).

        roundtrip_ctx - if the new_schema has been roundtripped through a database that doesn't support all Kart
            types, then some of them will have had to be approximated. If a roundtrip context is supplied,
            this function also removes changes in new_schema - restores new_schema to be more like `self` -
            for changes that were not requested by the user, but instead are simply side-effects of the roundtrip.
            See DefaultRoundtripContext.
        """
        # TODO - could prompt the user to help with more complex schema changes.
        old_cols = self.to_column_dicts()
        new_cols = new_schema.to_column_dicts()
        Schema.align_schema_cols(old_cols, new_cols, roundtrip_ctx=roundtrip_ctx)
        return Schema.from_column_dicts(new_cols)

    @classmethod
    def align_schema_cols(cls, old_cols, new_cols, roundtrip_ctx=None):
        """Same as align_to_self, but mutates new_cols list, instead of returning a new Schema object."""
        for old_col in old_cols:
            old_col["done"] = False
        for new_col in new_cols:
            new_col["done"] = False

        # Align columns by name + type - handles reordering.
        old_cols_by_name = {c["name"]: c for c in old_cols}
        for new_col in new_cols:
            cls._try_align(
                old_cols_by_name.get(new_col["name"]), new_col, roundtrip_ctx
            )

        # Align columns by position + type - handles renames.
        for old_col, new_col in zip(old_cols, new_cols):
            cls._try_align(old_col, new_col, roundtrip_ctx)

        for old_col in old_cols:
            del old_col["done"]
        for new_col in new_cols:
            ColumnSchema._normalise_dict(new_col)
            del new_col["done"]

        return new_cols

    @classmethod
    def _try_align(cls, old_col, new_col, roundtrip_ctx=None):
        if old_col is None or new_col is None:
            return False
        if old_col["done"] or new_col["done"]:
            return False
        if old_col.get("primaryKeyIndex") != new_col.get("primaryKeyIndex"):
            return False

        roundtrip_ctx = roundtrip_ctx or DefaultRoundtripContext
        if roundtrip_ctx.try_align_schema_col(old_col, new_col):
            # The two columns are similar enough that we can align their IDs.
            new_col["id"] = old_col["id"]
            old_col["done"] = True
            new_col["done"] = True
            return True
        else:
            return False

    def diff_types(self, new_schema):
        """Returns a dict showing which columns have been affected by which types of changes."""
        old_ids_list = [c.id for c in self]
        old_ids = set(old_ids_list)
        new_ids_list = [c.id for c in new_schema]
        new_ids = set(new_ids_list)

        inserts = new_ids - old_ids
        deletes = old_ids - new_ids
        position_updates = set()
        name_updates = set()
        type_updates = set()
        pk_updates = set()

        for new_index, new_col in enumerate(new_schema):
            col_id = new_col.id
            if col_id not in old_ids:
                continue
            old_col = self[col_id]

            old_index = old_ids_list.index(col_id)
            if old_index != new_index:
                position_updates.add(col_id)

            if old_col.name != new_col.name:
                name_updates.add(col_id)
            if (
                old_col.data_type != new_col.data_type
                or old_col.extra_type_info != new_col.extra_type_info
            ):
                type_updates.add(col_id)
            if old_col.pk_index != new_col.pk_index:
                pk_updates.add(col_id)

        return {
            "inserts": inserts,
            "deletes": deletes,
            "position_updates": position_updates,
            "name_updates": name_updates,
            "type_updates": type_updates,
            "pk_updates": pk_updates,
        }

    def diff_type_counts(self, new_schema):
        return {k: len(v) for k, v in self.diff_types(new_schema).items()}

    # These are the types that are stored in datasets.
    # Different types might be returned from the working copy DB driver, in which case, they must be adapted.
    EQUIVALENT_MSGPACK_TYPES = {
        "boolean": (bool,),
        "blob": (bytes,),
        "date": (str,),  # might be datetime.date from DB
        "float": (float, int),
        "geometry": (Geometry,),
        "integer": (int,),
        "interval": (str,),  # might be datetime.timedelta from DB
        "numeric": (str,),  # might be decimal.Decimal from DB
        "text": (str,),
        "time": (str,),  # might be datetime.time from DB
        "timestamp": (str,),  # might be datetime.datetime from DB
    }

    def validate_feature(self, feature, col_violations=None):
        """
        Returns True if the feature is valid, False if it has a schema violation.
        Populates col_violations dict with an example of a violation from each column, if one can be found.
        """
        if col_violations is None:
            return not any(
                self._find_column_violation(col, feature) is not None
                for col in self.columns
            )

        has_violation = bool(col_violations)
        for col in self.columns:
            if col.name in col_violations:
                # We have already output an error message for a different violation in this column,
                # and has_violation is already set to True. No need to investigate this column further.
                continue

            col_violation = self.find_column_violation(col, feature.get(col.name))
            if col_violation is not None:
                col_violations[col.name] = col_violation
                has_violation = True

        return not has_violation

    def find_column_violation(self, col, value):
        """
        Returns the error message for how the feature's value violates the given column schema.
        Returns None if the feature's value is compliant.
        """
        if value is None:
            return None

        col_type = col.data_type
        if type(value) not in self.EQUIVALENT_MSGPACK_TYPES[col_type]:
            return f"In column '{col.name}' value {repr(value)} doesn't match schema type {col_type}"

        do_find_violation = getattr(
            self, f"_find_{col_type}_violation", lambda col, value: None
        )
        return do_find_violation(col, value)

    @classmethod
    def _find_blob_violation(cls, col, value):
        length = col.extra_type_info.get("length")
        if not length:
            return None
        len_value = len(value)
        if len_value > length:
            if len_value > 100:
                value = value[:40] + b"....." + value[-40:]
            return f"In column '{col.name}' value {repr(value)} exceeds limit of {length} bytes"

    @classmethod
    def _find_date_violation(cls, col, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return f"In column '{col.name}' value {repr(value)} is not an ISO 8601 date ie YYYY-MM-DD"

    @classmethod
    def _find_integer_violation(cls, col, value):
        size = col.extra_type_info.get("size")
        if size and cls._signed_bit_length(value) > size:
            bounds = 2 ** (size - 1)
            return f"In column '{col.name}' value {repr(value)} does not fit into an int{size}: {-bounds} to {bounds-1}"

    _DATE_INTERVAL = r"(\d+Y)?(\d+M)?(\d+W)?(\d+D)?"
    _TIME_INTERVAL = r"(\d+H)?(\d+M)?(\d+(\.\d+)?S)?"
    _INTERVAL = rf"P{_DATE_INTERVAL}(T{_TIME_INTERVAL})?"

    @classmethod
    def _find_interval_violation(cls, col, value):
        if not re.fullmatch(cls._INTERVAL, value):
            return f"In column '{col.name}' value {repr(value)} is not an ISO 8601 duration ie PxYxMxDTxHxMxS"

    @classmethod
    def _signed_bit_length(cls, integer):
        if integer < 0:
            return (integer + 1).bit_length() + 1
        else:
            return integer.bit_length() + 1

    @classmethod
    def _find_text_violation(cls, col, value):
        length = col.extra_type_info.get("length")
        if not length:
            return None
        len_value = len(value)
        if len_value > length:
            if len_value > 100:
                value = value[:40] + "....." + value[-40:]
            return f"In column '{col.name}' value {repr(value)} exceeds limit of {length} characters"

    @classmethod
    def _find_time_violation(cls, col, value):
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}(\.\d+)?Z?", value):
            return f"In column '{col.name}' value {repr(value)} is not an ISO 8601 time ie hh:mm:ss.ssss"

    @classmethod
    def _find_timestamp_violation(cls, col, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?", value):
            return f"In column '{col.name}' value {repr(value)} is not an ISO 8601 UTC datetime ie YYYY-MM-DDThh:mm:ss.ssss"


class DefaultRoundtripContext:
    """
    DefaultRoundtripContext is appropriate in the case the new column hasn't been roundtripped through any storage
    format that is lossy - all of the original information has been preserved.
    """

    @classmethod
    def try_align_schema_col(cls, old_col_dict, new_col_dict):
        """
        This function is given two columns that are suspected of being different versions of the same column, since they
        have the same name or position. Its job is to decide if they are the same (or at least, they could be the same -
        we can't know for sure, as this is a heuristic). If they could be the same column, return True.

        For example:
        If old_col is int16 and new_col is int16 -> return True
        If old_col is int16 and new_col is int32 -> return True. We assume the user modified the column width.
        If old_col is a geometry and new_col is int32 -> return False.
        These must be different columns, since geometry data cannot become integer data.

        This is the default strategy - it needs to be redefined in the case where the new column has been roundtripped
        through a format that doesn't support every Kart type. For instance, GPKG doesn't have a numeric type. So, when
        new_col has been roundtripped through a GPKG, the following logic would be added:

        If old_col is numeric and new_col is text -> return True.

        But before returning True, any differences that are simply artifacts of the roundtrip should be removed.
        In this case, that would mean:
        - restore new_col's type to "numeric"
        - copy "precision" and "scale" attributes from old_col to new_col, since these will have been lost too.
        """

        return new_col_dict["dataType"] == old_col_dict["dataType"]
