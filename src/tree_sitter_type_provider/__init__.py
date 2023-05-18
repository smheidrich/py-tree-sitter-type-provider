from dataclasses import dataclass
from types import ModuleType
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Type,
    Union,
    cast,
)

import tree_sitter

from tree_sitter_type_provider.node_types import Branch as Branch
from tree_sitter_type_provider.node_types import Leaf as Leaf
from tree_sitter_type_provider.node_types import Node as Node
from tree_sitter_type_provider.node_types import NodeArgsType as NodeArgsType
from tree_sitter_type_provider.node_types import NodeFieldName as NodeFieldName
from tree_sitter_type_provider.node_types import NodeType as NodeType
from tree_sitter_type_provider.node_types import NodeTypeError as NodeTypeError
from tree_sitter_type_provider.node_types import NodeTypeName as NodeTypeName
from tree_sitter_type_provider.node_types import Point as Point


@dataclass
class ParseError(Exception, Branch):
    children: List[Node]
    contents: Optional[str] = None
    filename: Optional[str] = None

    @staticmethod
    def _point_to_str(point: Point) -> str:
        return f"line {point.line}, column {point.column}"

    def _range(self) -> str:
        if self.start_position.line == self.end_position.line:
            return f"on line {self.start_position.line} between column {self.start_position.column} and {self.end_position.column}"
        else:
            return f" between {self._point_to_str(self.start_position)} and {self._point_to_str(self.end_position)}"

    def _annotated_region(self) -> str:
        if self.contents:

            def _annotated_lines(
                lines: Sequence[str],
            ) -> Iterator[str]:
                for l, line in enumerate(lines):
                    yield line
                    is_first_line = l == 0
                    is_last_line = l == len(lines) - 1
                    start = self.start_position.column if is_first_line else 0
                    end = self.end_position.column if is_last_line else len(line)
                    annotation: List[str] = []
                    for c, _ in enumerate(line):
                        if c < start or end < c:
                            annotation.append(" ")
                        else:
                            annotation.append("^")
                    yield "".join(annotation)

            lines = self.contents.splitlines()
            lines = lines[self.start_position.line : self.end_position.line + 1]
            return "\n".join(_annotated_lines(lines))
        else:
            return self.text

    def __str__(self) -> str:
        return "".join(
            (
                f"Parse error ",
                f"in {self.filename} " if self.filename else "",
                f"{self._range()}:\n",
                f"{self._annotated_region()}\n",
            )
        )


class TreeSitterTypeProvider(ModuleType):
    def _node_type_name(
        self,
        node: Union[NodeTypeName, Node, NodeType, tree_sitter.Node],
    ) -> str:
        if isinstance(node, tree_sitter.Node):
            return cast(str, node.type)
        elif isinstance(node, Node):
            return node.type_name
        elif isinstance(node, NodeType):
            return node.type_name
        elif isinstance(node, str):
            return node
        else:
            raise NodeTypeError(
                f"Cannot get node type name for object of type {type(node)}", node
            )

    def _node_type(
        self,
        node: Union[NodeTypeName, Node, NodeType, tree_sitter.Node],
    ) -> NodeType:
        node_type_name = self._node_type_name(node)
        node_type = self._node_types_by_type.get(node_type_name, None)
        if node_type is None:
            raise NodeTypeError(f"Could not find node type {node_type_name}")
        return node_type

    def _node_has_children(
        self,
        node: Union[NodeTypeName, Node, NodeType, tree_sitter.Node],
    ) -> bool:
        node_type = self._node_type(node)
        # NOTE: Any node with content can have extra nodes,
        #       even if the node only has fields and no children.
        return node_type.has_content

    def _node_class(self, node: Union[Node, NodeType, tree_sitter.Node]) -> Type[Node]:
        node_type_name = self._node_type_name(node)
        cls = self._node_classes_by_type.get(node_type_name, None)
        if cls is None:
            raise NodeTypeError(f"Could not find node type {node_type_name}")
        return cls

    def _node_hash(self, tsnode: tree_sitter.Node) -> int:
        return hash((tsnode.start_byte, tsnode.end_byte))

    def from_tree_sitter(
        self,
        tsvalue: Union[tree_sitter.Tree, tree_sitter.Node, tree_sitter.TreeCursor],
        *,
        encoding: str = "utf-8",
        filename: Optional[str] = None,
        raise_parse_error: bool = False,
    ) -> Node:
        if isinstance(tsvalue, tree_sitter.Tree):
            tsvalue = tsvalue.root_node
        if isinstance(tsvalue, tree_sitter.Node):
            tsvalue = tsvalue.walk()
        return self._from_tree_cursor(
            tsvalue,
            encoding=encoding,
            filename=filename,
            raise_parse_error=raise_parse_error,
        )

    def _from_tree_cursor(
        self,
        tscursor: tree_sitter.TreeCursor,
        *,
        encoding: str = "utf-8",
        filename: Optional[str] = None,
        raise_parse_error: bool,
    ) -> Node:
        if tscursor.node.is_named:
            # Convert basic information.
            text: str = tscursor.node.text.decode(encoding)
            type_name: str = tscursor.node.type
            node_type: NodeType = self._node_type(type_name)
            start_position: Point = Point.from_tree_sitter(tscursor.node.start_point)
            end_position: Point = Point.from_tree_sitter(tscursor.node.end_point)

            # Convert children.
            fields: Dict[str, Union[None, Node, List[Node]]] = {}
            children: List[Node] = []

            def convert_child(tscursor: tree_sitter.TreeCursor) -> None:
                field_name = tscursor.current_field_name()
                child = self._from_tree_cursor(
                    tscursor,
                    encoding=encoding,
                    filename=filename,
                    raise_parse_error=raise_parse_error,
                )
                if field_name is None:
                    children.append(child)
                else:
                    if node_type.fields[field_name].multiple:
                        if field_name in fields:
                            field_value = fields[field_name]
                            if field_value is None:
                                fields[field_name] = child
                            elif isinstance(field_value, Node):
                                fields[field_name] = [field_value, child]
                            else:
                                field_value.append(child)
                        else:
                            fields[field_name] = [child]
                    else:
                        fields[field_name] = child

            if tscursor.goto_first_child():
                if tscursor.node.is_named:
                    convert_child(tscursor)
                while tscursor.goto_next_sibling():
                    if tscursor.node.is_named:
                        convert_child(tscursor)
                assert tscursor.goto_parent()

            # Handle optional fields.
            for field_name, field_type in node_type.fields.items():
                if not field_type.required and field_name not in fields:
                    if field_type.multiple:
                        fields[field_name] = []
                    else:
                        fields[field_name] = None

            # Create node instance
            kwargs: Dict[str, Union[str, Point, None, Node, List[Node]]] = {}
            kwargs["type_name"] = type_name
            kwargs["text"] = text
            kwargs["start_position"] = start_position
            kwargs["end_position"] = end_position
            if self._node_has_children(tscursor.node):
                kwargs["children"] = children
            kwargs.update(fields)

            # Return the node with its field name.
            if raise_parse_error and type_name == "ERROR":

                def _root_node(tsnode: tree_sitter.Node) -> str:
                    # TODO: node->tree isn't bound in tree_sitter_talon
                    if tsnode.parent:
                        return _root_node(tsnode.parent)
                    else:
                        return cast(str, tsnode.text.decode("utf-8"))

                contents = _root_node(tscursor.node)
                raise ParseError(
                    text=text,
                    type_name=type_name,
                    start_position=start_position,
                    end_position=end_position,
                    children=children,
                    contents=contents,
                    filename=filename,
                )
            else:
                return self._node_class(tscursor.node)(**kwargs)  # type: ignore[arg-type]
        else:
            raise TypeError(tscursor.node.type)

    def __init__(
        self,
        module_name: str,
        node_types: Sequence[NodeType],
        *,
        extra: Sequence[NodeTypeName] = (),
        as_class_name: Optional[Callable[[str], str]] = None,
        mixins: Sequence[type] = (),
        dataclass_kwargs: Dict[str, Any] = {},
    ):
        super().__init__(name=module_name)

        if as_class_name is None:

            def snake_to_pascal(node_type_name: str) -> str:
                return "".join(map(str.capitalize, node_type_name.split("_")))

            as_class_name = snake_to_pascal

        # Dictionary of named node types
        self._node_types_by_type: Dict[str, NodeType] = {}
        for node_type in node_types:
            if node_type.named:
                self._node_types_by_type[node_type.type_name] = node_type

        # Insert node types for extra nodes
        for type_name in extra:
            if type_name not in self._node_types_by_type:
                self._node_types_by_type[type_name] = NodeType(type_name, named=True)

        # Insert a node type for ERROR
        self._node_types_by_type["ERROR"] = NodeType(
            type_name="ERROR",
            named=True,
            children=NodeArgsType(
                multiple=True,
                required=True,
                types=list(node_types),
            ),
        )

        # Create the list of extra node types
        def _extra_node_types() -> Iterator[NodeType]:
            for type_name in extra:
                yield self._node_type(type_name)
            if "ERROR" not in extra:
                yield self._node_type("ERROR")

        self._extra: Sequence[NodeType] = tuple(_extra_node_types())

        # Dictionary of abstract classes
        self._node_bases_by_type: Dict[str, List[Type[Node]]] = {}
        for node_type in self._node_types_by_type.values():
            if node_type.is_abstract:
                abscls = node_type.as_type(
                    as_class_name=as_class_name,
                    base=Node,
                    mixins=mixins,
                    extra=self._extra,
                )
                abscls_name = as_class_name(node_type.type_name)
                setattr(self, abscls_name, abscls)
                for subtype in node_type.subtypes:
                    if subtype.named:
                        if subtype.type_name not in self._node_bases_by_type:
                            self._node_bases_by_type[subtype.type_name] = []
                        self._node_bases_by_type[subtype.type_name].append(abscls)

        # Dictionary of dataclasses
        self._node_classes_by_type: Dict[str, Type[Node]] = {}
        for node_type in self._node_types_by_type.values():
            if node_type.type_name == "ERROR":
                self._node_classes_by_type["ERROR"] = ParseError
                setattr(self, as_class_name(node_type.type_name), ParseError)
            else:
                bases = self._node_bases_by_type.get(node_type.type_name, ())
                cls = node_type.as_type(
                    mixins=tuple((*bases, *mixins)),
                    as_class_name=as_class_name,
                    extra=self._extra,
                    **dataclass_kwargs,
                )
                self._node_classes_by_type[node_type.type_name] = cls
                setattr(self, as_class_name(node_type.type_name), cls)
