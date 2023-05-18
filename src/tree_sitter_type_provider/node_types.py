import functools
import itertools
from dataclasses import dataclass, field, make_dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from dataclasses_json import DataClassJsonMixin, config, dataclass_json


class NodeTypeError(Exception):
    pass


@dataclass
class Point:
    line: int
    column: int

    @staticmethod
    def from_tree_sitter(tspoint: Tuple[int, int]) -> "Point":
        return Point(line=tspoint[0], column=tspoint[1])


NodeTypeName = str

NodeFieldName = str

AsClassName = Callable[[NodeTypeName], str]


@dataclass
class Node(DataClassJsonMixin):
    text: str
    type_name: NodeTypeName = field(metadata=config(field_name="type"))
    start_position: Point
    end_position: Point

    def is_equivalent(self, other: "Node") -> bool:
        try:
            self.assert_equivalent(other)
            return True
        except AssertionError:
            return False

    def assert_equivalent(self, other: "Node") -> None:
        pass


@dataclass
class Leaf(Node):
    pass


@dataclass
class Branch(Node):
    children: Union[None, Node, Sequence[Node]]


@dataclass
class SimpleNodeType(DataClassJsonMixin):
    type_name: NodeTypeName = field(metadata=config(field_name="type"))
    named: bool

    def is_extra(self, *, extra: Sequence["SimpleNodeType"]) -> bool:
        return any(self.type_name == other.type_name for other in extra)

    def as_typehint(self, *, as_class_name: AsClassName) -> Type[Node]:
        if self.named:
            return cast(type, as_class_name(self.type_name))
        raise ValueError(self)

    @staticmethod
    def many_as_typehint(
        simple_node_types: Sequence["SimpleNodeType"],
        *,
        as_class_name: AsClassName,
    ) -> Optional[Type[Node]]:
        Ts: List[type] = []
        for simple_node_type in simple_node_types:
            if simple_node_type.named:
                Ts.append(simple_node_type.as_typehint(as_class_name=as_class_name))

        if len(Ts) == 0:
            return None

        if len(Ts) == 1:
            return Ts[0]

        return functools.reduce(lambda R, T: cast(type, Union[R, T]), Ts)


@dataclass_json
@dataclass
class NodeArgsType:
    multiple: bool = False
    required: bool = False
    types: List[SimpleNodeType] = field(default_factory=list)

    @property
    def named(self) -> bool:
        return any(type.named for type in self.types)

    def __bool__(self) -> bool:
        return bool(self.types)

    def assert_equivalent(
        self,
        node1: Union[None, Node, Sequence[Node]],
        node2: Union[None, Node, Sequence[Node]],
    ) -> None:
        if self.named:
            if node1 is None:
                assert node2 is None
            elif isinstance(node1, Node):
                assert isinstance(node2, Node)
                node1.assert_equivalent(node2)
            else:
                assert isinstance(node2, Sequence)
                for child1, child2 in itertools.zip_longest(node1, node2):
                    assert isinstance(child1, Node)
                    assert isinstance(child2, Node)
                    child1.assert_equivalent(child2)

    def as_typehint(
        self,
        *,
        is_field: bool,
        as_class_name: AsClassName,
        extra: Sequence[SimpleNodeType],
    ) -> Optional[Type[Node]]:
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
                return List[T]  # type: ignore
            else:
                if self.required:
                    return T
                else:
                    return Optional[T]  # type: ignore
        else:
            return None


@dataclass
class NodeType(SimpleNodeType):
    fields: Dict[NodeFieldName, NodeArgsType] = field(default_factory=dict)
    children: NodeArgsType = field(default_factory=NodeArgsType)
    subtypes: List[SimpleNodeType] = field(default_factory=list)

    def __post_init__(self, **kwargs: Any) -> None:
        assert not (
            self.is_abstract and self.has_content
        ), "Nodes can have either fields and children or subtypes, but not both."

    @property
    def is_abstract(self) -> bool:
        return len(self.subtypes) > 0

    @property
    def has_content(self) -> bool:
        return len(self.fields) > 0 or bool(self.children)

    def assert_equivalent(self, node1: Node, node2: Node) -> None:
        if self.has_content:
            # compare children
            if hasattr(node1, "children"):
                assert hasattr(node2, "children")
                children1 = getattr(node1, "children")
                children2 = getattr(node2, "children")
                self.children.assert_equivalent(children1, children2)
            # compare fields
            for field_name, field_type in self.fields.items():
                if hasattr(node1, field_name):
                    assert hasattr(node2, field_name)
                    field1 = getattr(node1, field_name)
                    field2 = getattr(node2, field_name)
                    field_type.assert_equivalent(field1, field2)
                else:
                    assert not hasattr(node2, field_name)
        else:
            assert node1.text == node2.text

    def as_type(
        self,
        *,
        as_class_name: AsClassName,
        mixins: Sequence[type] = (),
        extra: Sequence[SimpleNodeType],
        **kwargs: Any,
    ) -> Type[Node]:
        if self.named:
            cls_name = as_class_name(self.type_name)
            if self.is_abstract:
                # TODO: should be a dynamic type alias
                return type(cls_name, (Node,), {})
            else:
                fields: Dict[NodeFieldName, Type[Any]] = {}

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
                        fields["children"] = cast(type, None)

                # Create bases for dataclass
                base: Type[Node]
                if self.is_abstract:
                    base = Node
                elif self.has_content:
                    base = Branch
                else:
                    base = Leaf

                # Create implementation of is_equivalent
                node_type = self

                def _assert_equivalent(self: Node, other: Node) -> None:
                    node_type.assert_equivalent(self, other)

                # Create and return dataclass
                return make_dataclass(
                    cls_name=cls_name,
                    fields=fields.items(),
                    bases=(base, *mixins),
                    namespace={
                        "assert_equivalent": _assert_equivalent,
                    },
                    **kwargs,
                )
        else:
            raise ValueError(self)
