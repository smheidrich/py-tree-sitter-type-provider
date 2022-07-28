import dataclasses
import dataclasses_json
import functools
import typing
import collections.abc


class NodeTypeError(Exception):
    pass


@dataclasses.dataclass
class Point:
    row: int
    column: int


NodeTypeName: typing.TypeAlias = str

NodeFieldName: typing.TypeAlias = str

AsClassName: typing.TypeAlias = collections.abc.Callable[[NodeTypeName], str]


@dataclasses.dataclass
class Node(dataclasses_json.DataClassJsonMixin):
    text: str
    type_name: NodeTypeName = dataclasses.field(
        metadata=dataclasses_json.config(field_name="type")
    )
    start_position: Point
    end_position: Point


@dataclasses.dataclass
class Leaf(Node):
    pass


@dataclasses.dataclass
class Branch(Node):
    children: typing.Union[None, Node, typing.Sequence[Node]]


@dataclasses_json.dataclass_json
@dataclasses.dataclass
class SimpleNodeType:
    type_name: NodeTypeName = dataclasses.field(
        metadata=dataclasses_json.config(field_name="type")
    )
    named: bool

    def is_extra(self, *, extra: typing.Sequence["SimpleNodeType"]) -> bool:
        return any(self.type_name == other.type_name for other in extra)

    def as_typehint(self, *, as_class_name: AsClassName) -> type[Node]:
        if self.named:
            return typing.cast(type, as_class_name(self.type_name))
        raise ValueError(self)

    @staticmethod
    def many_as_typehint(
        simple_node_types: typing.Sequence["SimpleNodeType"],
        *,
        as_class_name: AsClassName,
    ) -> typing.Optional[type[Node]]:
        Ts: list[type] = []
        for simple_node_type in simple_node_types:
            if simple_node_type.named:
                Ts.append(simple_node_type.as_typehint(as_class_name=as_class_name))

        if len(Ts) == 0:
            return None

        if len(Ts) == 1:
            return Ts[0]

        return functools.reduce(lambda R, T: typing.cast(type, typing.Union[R, T]), Ts)


@dataclasses_json.dataclass_json
@dataclasses.dataclass
class NodeArgsType:
    multiple: bool = False
    required: bool = False
    types: list[SimpleNodeType] = dataclasses.field(default_factory=list)

    @property
    def named(self) -> bool:
        return any(type.named for type in self.types)

    def __bool__(self) -> bool:
        return bool(self.types)

    def as_typehint(
        self,
        *,
        is_field: bool,
        as_class_name: AsClassName,
        extra: typing.Sequence[SimpleNodeType],
    ) -> typing.Optional[type[Node]]:
        if is_field:
            types_with_extra = tuple(self.types)
            multiple_with_extra = self.multiple
        else:
            # Add the extra nodes into the possible children
            types_with_extra = tuple((*self.types, *extra))
            # If there are extra nodes, a node can have any number of children
            multiple_with_extra = bool(extra) or self.multiple
        T = SimpleNodeType.many_as_typehint(
            types_with_extra, as_class_name=as_class_name
        )
        if T is not None:
            if multiple_with_extra:
                return list[T]  # type: ignore
            else:
                if self.required:
                    return T
                else:
                    return typing.Optional[T]  # type: ignore
        else:
            return None


@dataclasses_json.dataclass_json
@dataclasses.dataclass
class NodeType(SimpleNodeType):
    fields: dict[NodeFieldName, NodeArgsType] = dataclasses.field(default_factory=dict)
    children: NodeArgsType = dataclasses.field(default_factory=NodeArgsType)
    subtypes: list[SimpleNodeType] = dataclasses.field(default_factory=list)

    def __post_init__(self, **kwargs):
        assert not (
            self.is_abstract and self.has_content
        ), "Nodes can have either fields and children or subtypes, but not both."

    @property
    def is_abstract(self) -> bool:
        return len(self.subtypes) > 0

    @property
    def has_content(self) -> bool:
        return len(self.fields) > 0 or bool(self.children)

    def as_type(
        self,
        *,
        as_class_name: AsClassName,
        mixins: typing.Sequence[type] = (),
        extra: typing.Sequence[SimpleNodeType],
        **kwargs,
    ) -> type[Node]:
        if self.named:
            cls_name = as_class_name(self.type_name)
            if self.is_abstract:
                # TODO: should be a dynamic type alias
                return type(cls_name, (Node,), {})
            else:
                fields: dict[NodeFieldName, type] = {}

                # Create fields for dataclass
                for field_name, field in self.fields.items():
                    if field.named:
                        field_type = field.as_typehint(
                            is_field=True, as_class_name=as_class_name, extra=extra
                        )
                        if field_type is not None:
                            fields[field_name] = field_type

                # Create children for dataclass
                if self.has_content:
                    children_type = self.children.as_typehint(
                        is_field=False, as_class_name=as_class_name, extra=extra
                    )
                    if children_type:
                        fields["children"] = children_type
                    else:
                        fields["children"] = typing.cast(type, None)

                # Create bases for dataclass
                base: type[Node]
                if self.is_abstract:
                    base = Node
                elif self.has_content:
                    base = Branch
                else:
                    base = Leaf

                # Create and return dataclass
                return dataclasses.make_dataclass(
                    cls_name=cls_name,
                    fields=fields.items(),
                    bases=(base, *mixins),
                    **kwargs,
                )
        else:
            raise ValueError(self)
