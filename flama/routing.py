import typing

import asyncio
import inspect
import logging
import marshmallow
import starlette.routing
from functools import wraps
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.routing import BaseRoute, Match, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from flama import http, websockets
from flama.components import Component
from flama.responses import APIResponse, Response
from flama.types import Field, FieldLocation, HTTPMethod, OptBool, OptFloat, OptInt, OptStr
from flama.utils import is_marshmallow_dataclass, is_marshmallow_schema
from flama.validation import get_output_schema

if typing.TYPE_CHECKING:
    from flama.resources import BaseResource

__all__ = ["Route", "WebSocketRoute", "Router", "APIRouter"]

logger = logging.getLogger(__name__)


FieldsMap = typing.Dict[str, Field]
MethodsMap = typing.Dict[str, FieldsMap]

PATH_SCHEMA_MAPPING = {
    inspect.Signature.empty: lambda *args, **kwargs: None,
    int: marshmallow.fields.Integer,
    float: marshmallow.fields.Number,
    str: marshmallow.fields.String,
    bool: marshmallow.fields.Boolean,
    http.PathParam: marshmallow.fields.String,
}

QUERY_SCHEMA_MAPPING = {
    inspect.Signature.empty: lambda *args, **kwargs: None,
    int: marshmallow.fields.Integer,
    float: marshmallow.fields.Number,
    bool: marshmallow.fields.Boolean,
    str: marshmallow.fields.String,
    OptInt: marshmallow.fields.Integer,
    OptFloat: marshmallow.fields.Number,
    OptBool: marshmallow.fields.Boolean,
    OptStr: marshmallow.fields.String,
    http.QueryParam: marshmallow.fields.String,
}


class FieldsMixin:
    def _get_fields(
        self, router: "Router"
    ) -> typing.Tuple[MethodsMap, MethodsMap, typing.Dict[str, Field], typing.Dict[str, typing.Any]]:
        query_fields: MethodsMap = {}
        path_fields: MethodsMap = {}
        body_field: typing.Dict[str, Field] = {}
        output_field: typing.Dict[str, typing.Any] = {}

        if hasattr(self, "methods") and self.methods is not None:
            if inspect.isclass(self.endpoint):  # HTTP endpoint
                methods = [(m, getattr(self.endpoint, m.lower() if m != "HEAD" else "get")) for m in self.methods]
            else:  # HTTP function
                methods = [(m, self.endpoint) for m in self.methods] if self.methods else []
        else:  # Websocket
            methods = [("GET", self.endpoint)]

        for m, h in methods:
            query_fields[m], path_fields[m], body_field[m], output_field[m] = self._get_fields_from_handler(h, router)

        return query_fields, path_fields, body_field, output_field

    def _get_parameters_from_handler(
        self, handler: typing.Callable, router: "Router"
    ) -> typing.Dict[str, inspect.Parameter]:
        parameters = {}

        for name, parameter in inspect.signature(handler).parameters.items():
            for component in router.components:
                if component.can_handle_parameter(parameter):
                    parameters.update(self._get_parameters_from_handler(component.resolve, router))
                    break
            else:
                parameters[name] = parameter

        return parameters

    def _get_fields_from_handler(
        self, handler: typing.Callable, router: "Router"
    ) -> typing.Tuple[FieldsMap, FieldsMap, Field, typing.Any]:
        query_fields: FieldsMap = {}
        path_fields: FieldsMap = {}
        body_field: Field = None
        request_schemas = getattr(handler, '_request_schemas', None) or {}
        response_schema = getattr(handler, '_response_schema', None)

        # Iterate over all params
        for name, param in self._get_parameters_from_handler(handler, router).items():
            # If schema override exists, update the parameter's annotation
            schema_override = request_schemas.get(name)
            if schema_override:
                param = param.replace(annotation=schema_override)

            if name in ("self", "cls"):
                continue
            # Matches as path param
            if name in self.param_convertors.keys():
                try:
                    schema = PATH_SCHEMA_MAPPING[param.annotation]
                except KeyError:
                    schema = marshmallow.fields.String

                path_fields[name] = Field(
                    name=name, location=FieldLocation.path, schema=schema(required=True), required=True
                )
            # Matches as query param
            elif param.annotation in QUERY_SCHEMA_MAPPING:
                if param.annotation in (OptInt, OptFloat, OptBool, OptStr) or param.default is not param.empty:
                    required = False
                    kwargs = {"missing": param.default if param.default is not param.empty else None}
                else:
                    required = True
                    kwargs = {"required": True}

                query_fields[name] = Field(
                    name=name,
                    location=FieldLocation.query,
                    schema=QUERY_SCHEMA_MAPPING[param.annotation](**kwargs),
                    required=required,
                )
            # Body params
            elif is_marshmallow_schema(param.annotation):
                body_field = Field(name=name, location=FieldLocation.body, schema=param.annotation())
            # Handle marshmallow-dataclass
            elif is_marshmallow_dataclass(param.annotation):
                body_field = Field(name=name, location=FieldLocation.body, schema=param.annotation.Schema())

        output_field = response_schema if response_schema else inspect.signature(handler).return_annotation
        if is_marshmallow_dataclass(output_field):
            output_field = output_field.Schema

        return query_fields, path_fields, body_field, output_field


class Route(starlette.routing.Route, FieldsMixin):
    def __init__(self, path: str, endpoint: typing.Callable, router: "Router", *args, status_code: int = 200, **kwargs):
        super().__init__(path, endpoint=endpoint, **kwargs)

        # Replace function with another wrapper that uses the injector
        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            self.app = self.endpoint_wrapper(endpoint)

        if self.methods is None:
            self.methods = [m for m in HTTPMethod.__members__.keys() if hasattr(self, m.lower())]

        self.query_fields, self.path_fields, self.body_field, self.output_field = self._get_fields(router)
        self.status_code = status_code

    def endpoint_wrapper(self, endpoint: typing.Callable) -> ASGIApp:
        """
        Wraps a http function into ASGI application.
        """

        @wraps(endpoint)
        async def _app(scope: Scope, receive: Receive, send: Send) -> None:
            app = scope["app"]

            route, route_scope = app.router.get_route_from_scope(scope)

            state = {
                "scope": scope,
                "receive": receive,
                "send": send,
                "exc": None,
                "app": app,
                "path_params": route_scope["path_params"],
                "route": route,
                "request": http.Request(scope, receive),
            }

            try:
                injected_func = await app.injector.inject(endpoint, state)
                background_task = next(
                    (v for k, v in state.items() if k.startswith('backgroundtasks:') and isinstance(v, BackgroundTask)),
                    None
                )

                if asyncio.iscoroutinefunction(endpoint):
                    response = await injected_func()
                else:
                    response = await run_in_threadpool(injected_func)

                # Wrap response data with a proper response class
                if isinstance(response, (dict, list)):
                    response = APIResponse(
                        content=response,
                        schema=get_output_schema(endpoint),
                        background=background_task,
                        status_code=self.status_code,
                    )
                elif isinstance(response, str):
                    response = APIResponse(
                        content=response,
                        background=background_task,
                        status_code=self.status_code,
                    )
                elif response is None:
                    response = APIResponse(
                        content="",
                        background=background_task,
                        status_code=self.status_code,
                    )
                elif not isinstance(response, Response):
                    schema = get_output_schema(endpoint)
                    if schema is not None:
                        response = APIResponse(
                            content=response,
                            schema=get_output_schema(endpoint),
                            background=background_task,
                            status_code=self.status_code,
                        )
            except Exception:
                logger.exception("Error building response")
                raise

            await response(scope, receive, send)

        return _app


class WebSocketRoute(starlette.routing.WebSocketRoute, FieldsMixin):
    def __init__(self, path: str, endpoint: typing.Callable, router: "Router", *args, **kwargs):
        super().__init__(path, endpoint=endpoint, **kwargs)

        # Replace function with another wrapper that uses the injector
        if inspect.isfunction(endpoint):
            self.app = self.endpoint_wrapper(endpoint)

        self.query_fields, self.path_fields, self.body_field, self.output_field = self._get_fields(router)

    def endpoint_wrapper(self, endpoint: typing.Callable) -> ASGIApp:
        """
        Wraps websocket function into ASGI application.
        """

        @wraps(endpoint)
        async def _app(scope: Scope, receive: Receive, send: Send) -> None:
            app = scope["app"]

            route, route_scope = app.router.get_route_from_scope(scope)

            state = {
                "scope": scope,
                "receive": receive,
                "send": send,
                "exc": None,
                "app": app,
                "path_params": route_scope["path_params"],
                "route": route,
                "websocket": websockets.WebSocket(scope, receive, send),
            }

            try:
                injected_func = await app.injector.inject(endpoint, state)

                kwargs = scope.get("kwargs", {})
                await injected_func(**kwargs)
            except Exception:
                logger.exception("Error building response")
                raise

        return _app


class Router(starlette.routing.Router):
    def __init__(self, components: typing.Optional[typing.List[Component]] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if components is None:
            components = []

        self.components = components

    def add_route(
        self,
        path: str,
        endpoint: typing.Callable,
        methods: typing.List[str] = None,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        request_schemas: typing.Dict[str, marshmallow.Schema] = None,
    ):
        # If @schemas is used, it has precedence
        # i.e.:
        # @app.route('/', arg1: FooSchema)
        # @app.schemas('/', arg2: BarSchema, response_schema=FooBarSchema)
        # def endpoint(arg1, arg2):
        #       pass
        if getattr(endpoint, '_request_schemas', None):
            merged_schemas = request_schemas.copy()
            merged_schemas.update(endpoint._request_schemas)
            endpoint._request_schemas = merged_schemas
        else:
            endpoint._request_schemas = request_schemas

        if getattr(endpoint, '_response_schema', None) is None:
            endpoint._response_schema = response_schema

        self.routes.append(
            Route(
                path,
                endpoint=endpoint,
                methods=methods, name=name,
                include_in_schema=include_in_schema,
                router=self,
                status_code=status_code
            )
        )

    def add_websocket_route(self, path: str, endpoint: typing.Callable, name: str = None):
        self.routes.append(WebSocketRoute(path, endpoint=endpoint, name=name, router=self))

    def add_resource(self, path: str, resource: "BaseResource"):
        # Handle class or instance objects
        if inspect.isclass(resource):  # noqa
            resource = resource()

        for name, route in resource.routes.items():
            route_path = path + resource._meta.name + route._meta.path
            route_func = getattr(resource, name)
            name = route._meta.name if route._meta.name is not None else f"{resource._meta.name}-{route.__name__}"
            self.add_route(route_path, route_func, route._meta.methods, name, **route._meta.kwargs)

    def mount(self, path: str, app: ASGIApp, name: str = None) -> None:
        if isinstance(app, Router):
            app.components = self.components

        path = path.rstrip("/")
        route = Mount(path, app=app, name=name)
        self.routes.append(route)

    def route(
        self,
        path: str,
        methods: typing.List[str] = None,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        request_schemas: typing.Dict[str, marshmallow.Schema] = None,
    ) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_route(
                path,
                func,
                methods=methods,
                name=name,
                include_in_schema=include_in_schema,
                status_code=status_code,
                response_schema=response_schema,
                request_schemas=request_schemas
            )
            return func

        return decorator

    def websocket_route(self, path: str, name: str = None) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_websocket_route(path, func, name=name)
            return func

        return decorator

    def resource(self, path: str) -> typing.Callable:
        def decorator(resource: "BaseResource") -> "BaseResource":
            self.add_resource(path, resource=resource)
            return resource

        return decorator

    def get_route_from_scope(self, scope, mounted=False) -> typing.Tuple[Route, typing.Optional[typing.Dict]]:
        partial = None

        for route in self.routes:
            if isinstance(route, Mount):
                path = scope.get("path", "")
                root_path = scope.pop("root_path", "")
                if not mounted:
                    scope["path"] = root_path + path

            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)

                if isinstance(route, Mount):
                    if mounted:
                        scope["root_path"] = root_path + child_scope.get("root_path", "")
                    route, mount_scope = route.app.get_route_from_scope(scope, mounted=True)
                    return route, mount_scope

                return route, scope
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            scope.update(partial_scope)
            return partial, scope

        return self.not_found, None

    def get(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["GET"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def put(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["PUT"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def post(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["POST"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def delete(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["DELETE"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def options(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["OPTIONS"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def head(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["HEAD"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def patch(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["PATCH"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )

    def trace(
        self,
        path: str,
        name: str = None,
        include_in_schema: bool = True,
        status_code: int = 200,
        response_schema: marshmallow.Schema = None,
        **request_schemas: typing.Dict[str, marshmallow.Schema]
    ) -> typing.Callable:
        return self.route(
            path=path,
            methods=["TRACE"],
            name=name,
            include_in_schema=include_in_schema,
            status_code=status_code,
            response_schema=response_schema,
            request_schemas=request_schemas,
        )


class APIRouter(Router):
    def __init__(
        self,
        prefix: str = "",
        name: str = "",
        components: typing.Optional[typing.List[Component]] = None,
        routes: typing.Sequence[BaseRoute] = None,
        redirect_slashes: bool = True,
        default: ASGIApp = None,
        on_startup: typing.Sequence[typing.Callable] = None,
        on_shutdown: typing.Sequence[typing.Callable] = None,
        lifespan: typing.Callable[[typing.Any], typing.AsyncGenerator] = None,
        *args,
        **kwargs
    ):
        super().__init__(
            components=components,
            routes=routes,
            redirect_slashes=redirect_slashes,
            default=default,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            lifespan=lifespan,
            *args,
            **kwargs
        )

        self.prefix = prefix
        self.name = name

    def register_router(self, router: 'APIRouter'):
        assert isinstance(router, APIRouter), "Registered router must be an instance of APIRouter"
        self.mount(router.prefix, app=router, name=router.name)
