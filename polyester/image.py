import asyncio
import os
import sys
from typing import Dict

from .async_utils import retry
from .config import config, logger
from .exception import RemoteException
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIMEOUT
from .mount import get_sha256_hex_from_content  # TODO: maybe not
from .object import Object, requires_create
from .proto import api_pb2


def _make_bytes(s):
    assert type(s) in (str, bytes)
    return s.encode("ascii") if type(s) is str else s


class Layer(Object):
    def __init__(
        self,
        tag=None,
        base_layers={},
        dockerfile_commands=[],
        context_files={},
        must_create=False,
        local=False,
    ):
        dockerfile_commands = [_make_bytes(s) for s in dockerfile_commands]

        # Construct the local id
        local_id_args = []
        for docker_tag, layer in base_layers.items():
            local_id_args.append("b:%s:(%s)" % (docker_tag, layer.args.local_id))
        local_id_args.append("c:%s" % get_sha256_hex_from_content(b"\n".join(dockerfile_commands)))
        for filename, content in context_files.items():
            local_id_args.append("f:%s:%s" % (filename, get_sha256_hex_from_content(content)))

        super().__init__(
            args=dict(
                local_id=",".join(local_id_args),
                tag=tag,
                base_layers=base_layers,
                dockerfile_commands=dockerfile_commands,
                context_files=context_files,
                must_create=must_create,
                local=local,
            )
        )

    async def create_or_get(self):
        if self.args.tag:
            req = api_pb2.LayerGetByTagRequest(tag=self.args.tag)
            resp = await self.client.stub.LayerGetByTag(req)
            layer_id = resp.layer_id

        else:
            # Recursively build base layers
            base_layer_objs = await asyncio.gather(
                *(self.session.create_or_get_object(layer) for layer in self.args.base_layers.values())
            )
            base_layers_pb2s = [
                api_pb2.BaseLayer(docker_tag=docker_tag, layer_id=layer.object_id)
                for docker_tag, layer in zip(self.args.base_layers.keys(), base_layer_objs)
            ]

            context_file_pb2s = [
                api_pb2.LayerContextFile(filename=filename, data=data)
                for filename, data in self.args.context_files.items()
            ]

            layer_definition = api_pb2.Layer(
                base_layers=base_layers_pb2s,
                dockerfile_commands=self.args.dockerfile_commands,
                context_files=context_file_pb2s,
                local_id=self.args.local_id,
                local=self.args.local,
            )

            req = api_pb2.LayerGetOrCreateRequest(
                session_id=self.session.session_id,
                layer=layer_definition,
                must_create=self.args.must_create,
            )
            resp = await self.client.stub.LayerGetOrCreate(req)
            layer_id = resp.layer_id

        logger.debug("Waiting for layer %s" % layer_id)
        while True:
            request = api_pb2.LayerJoinRequest(
                layer_id=layer_id,
                timeout=BLOCKING_REQUEST_TIMEOUT,
                session_id=self.session.session_id,
            )
            response = await retry(self.client.stub.LayerJoin)(request, timeout=GRPC_REQUEST_TIMEOUT)
            if not response.result.status:
                continue
            elif response.result.status == api_pb2.GenericResult.Status.FAILURE:
                raise RemoteException(response.result.exception)
            elif response.result.status == api_pb2.GenericResult.Status.SUCCESS:
                break
            else:
                raise RemoteException("Unknown status %s!" % response.result.status)

        return layer_id

    @requires_create
    async def set_tag(self, tag):
        req = api_pb2.LayerSetTagRequest(layer_id=self.object_id, tag=tag)
        await self.client.stub.LayerSetTag(req)

    def is_inside(self):
        # This is used from inside of containers to know whether this container is active or not
        return os.getenv("POLYESTER_IMAGE_LOCAL_ID") == self.args.local_id


class EnvDict(Object):
    def __init__(self, env_dict):
        super().__init__(
            args=dict(
                env_dict=env_dict,
            )
        )

    async def create_or_get(self):
        req = api_pb2.EnvDictCreateRequest(session_id=self.session.session_id, env_dict=self.args.env_dict)
        resp = await self.client.stub.EnvDictCreate(req)
        return resp.env_dict_id


def get_python_version():
    return config["image_python_version"] or "%d.%d.%d" % sys.version_info[:3]


class DebianSlim(Layer):
    def __init__(self, python_version=None):
        if python_version is None:
            python_version = get_python_version()
        tag = "python-%s-slim-buster-base" % python_version
        self.python_version = python_version
        super().__init__(tag=tag)

    def add_python_packages(self, python_packages, find_links=None):
        find_links_arg = f"-f {find_links}" if find_links else ""

        layer = Layer(
            base_layers={
                "base": self,
                "builder": Layer(tag="python-%s-slim-buster-builder" % self.python_version),
            },
            dockerfile_commands=[
                "FROM builder as builder-vehicle",
                f"RUN pip wheel {' '.join(python_packages)} -w /tmp/wheels {find_links_arg}",
                "FROM base",
                "COPY --from=builder-vehicle /tmp/wheels /tmp/wheels",
                "RUN pip install /tmp/wheels/*",
                "RUN rm -rf /tmp/wheels",
            ],
        )
        return layer

    def run_commands(self, commands):
        layer = Layer(
            base_layers={"base": self},
            dockerfile_commands=["FROM base"] + ["RUN " + command for command in commands],
        )
        return layer


debian_slim = DebianSlim()
base_image = debian_slim
