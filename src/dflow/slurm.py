import copy
import os
import re
from typing import Dict, List, Union

from .executor import Executor, RemoteExecutor
from .io import (PVC, InputArtifact, InputParameter, OutputArtifact,
                 OutputParameter)
from .op_template import ScriptOPTemplate, ShellOPTemplate
from .resource import Resource
from .step import Step
from .steps import Steps

try:
    import yaml
    from argo.workflows.client import (V1alpha1ResourceTemplate,
                                       V1HostPathVolumeSource, V1Volume,
                                       V1VolumeMount)
except Exception:
    pass


class SlurmJob(Resource):
    def __init__(self, header="", node_selector=None, prepare=None,
                 results=None, map_tmp_dir=True, workdir=".",
                 remote_command=None):
        self.header = header
        self.action = "create"
        self.success_condition = "status.status == Succeeded"
        self.failure_condition = "status.status == Failed"
        self.node_selector = node_selector
        self.prepare = prepare
        self.results = results
        self.map_tmp_dir = map_tmp_dir
        self.workdir = workdir
        if isinstance(remote_command, str):
            remote_command = [remote_command]
        self.remote_command = remote_command

    def get_manifest(self, template):
        remote_command = template.command if self.remote_command is None else \
            self.remote_command
        map_cmd = " | sed \"s#/tmp#$(pwd)/tmp#g\" " if self.map_tmp_dir else ""
        manifest = {
            "apiVersion": "wlm.sylabs.io/v1alpha1",
            "kind": "SlurmJob",
            "metadata": {
                "name": "{{pod.name}}"
            },
            "spec": {
                "batch": self.header + "\nmkdir -p %s\ncd %s\ncat <<EOF %s |"
                " %s\n%s\nEOF" % (self.workdir, self.workdir, map_cmd,
                                  " ".join(remote_command), template.script)
            }
        }
        if self.node_selector is not None:
            manifest["spec"]["nodeSelector"] = self.node_selector
        if self.prepare is not None:
            manifest["spec"]["prepare"] = self.prepare
        if self.results is not None:
            manifest["spec"]["results"] = self.results
        return yaml.dump(manifest, default_style="|")


class SlurmJobTemplate(Executor):
    """
    Slurm job template

    Args:
        header: header for Slurm job
        node_selector: node selector
        prepare_image: image for preparing data
        collect_image: image for collecting results
        workdir: remote working directory
        remote_command: command for running the script remotely
    """

    def __init__(
            self,
            header: str = "",
            node_selector: Dict[str, str] = None,
            prepare_image: str = "alpine:latest",
            collect_image: str = "alpine:latest",
            workdir: str = "dflow/workflows/{{workflow.name}}/{{pod.name}}",
            remote_command: Union[str, List[str]] = None,
    ) -> None:
        self.header = header
        self.node_selector = node_selector
        self.prepare_image = prepare_image
        self.collect_image = collect_image
        self.workdir = workdir
        if isinstance(remote_command, str):
            remote_command = [remote_command]
        self.remote_command = remote_command

    def render(self, template):
        new_template = Steps(template.name + "-slurm")
        for art_name in template.inputs.artifacts:
            new_template.inputs.artifacts[art_name] = InputArtifact(
                name=art_name)
        for par_name in template.inputs.parameters:
            new_template.inputs.parameters[par_name] = InputParameter(
                name=par_name)
        prepare = None
        results = None

        # With using host path here, care should be taken for which node the
        # pod scheduled to
        if template.inputs.artifacts:
            volume = V1Volume(name="workdir", host_path=V1HostPathVolumeSource(
                path="/tmp/{{pod.name}}", type="DirectoryOrCreate"))
            mount = V1VolumeMount(name="workdir", mount_path="/workdir")
            script = ""
            for art in template.inputs.artifacts.values():
                script += "mkdir -p /workdir/%s\n" % os.path.dirname(art.path)
                script += "cp -r %s /workdir/%s\n" % (art.path, art.path)
            prepare_template = ShellOPTemplate(
                name=new_template.name + "-prepare", image=self.prepare_image,
                script=script, volumes=[volume], mounts=[mount])
            for name in template.inputs.parameters.keys():
                if name[:6] == "dflow_":
                    prepare_template.inputs.parameters[name] = \
                        InputParameter(
                        value="{{inputs.parameters.%s}}" % name)
            prepare_template.inputs.artifacts = copy.deepcopy(
                template.inputs.artifacts)
            prepare_template.outputs.parameters["dflow_vol_path"] = \
                OutputParameter(value="/tmp/{{pod.name}}")
            artifacts = {}
            for art_name in template.inputs.artifacts:
                artifacts[art_name] = new_template.inputs.artifacts[art_name]
            prepare_step = Step(
                "slurm-prepare", template=prepare_template,
                artifacts=artifacts)
            new_template.add(prepare_step)

            prepare = {
                "to": self.workdir,
                "mount": {
                    "name": "workdir",
                    "hostPath": {
                        "path": "{{inputs.parameters.dflow_vol_path}}",
                        "type": "DirectoryOrCreate"
                    }
                }
            }

        if template.outputs.parameters or template.outputs.artifacts:
            results = {
                "from": "%s/workdir" % self.workdir,
                "mount": {
                    "name": "mnt",
                    "hostPath": {
                        "path": "/tmp/{{pod.name}}",
                        "type": "DirectoryOrCreate"
                    }
                }
            }

        slurm_job = SlurmJob(
            header=self.header, node_selector=self.node_selector,
            prepare=prepare, results=results,
            workdir="%s/workdir" % self.workdir,
            remote_command=self.remote_command)
        run_template = ScriptOPTemplate(
            name=new_template.name + "-run",
            resource=V1alpha1ResourceTemplate(
                action=slurm_job.action,
                success_condition=slurm_job.success_condition,
                failure_condition=slurm_job.failure_condition,
                manifest=slurm_job.get_manifest(template=template)))
        run_template.inputs.parameters = copy.deepcopy(
            template.inputs.parameters)
        parameters = {}
        for par_name in template.inputs.parameters:
            parameters[par_name] = "{{inputs.parameters.%s}}" % par_name
        if prepare:
            run_template.inputs.parameters["dflow_vol_path"] = InputParameter()
            parameters["dflow_vol_path"] = \
                "{{steps.slurm-prepare.outputs.parameters.dflow_vol_path}}"
        if results:
            run_template.outputs.parameters["dflow_vol_path"] = \
                OutputParameter(value="/tmp/{{pod.name}}")
        run_step = Step("slurm-run", template=run_template,
                        parameters=parameters)
        new_template.add(run_step)

        if results:
            volume = V1Volume(name="mnt", host_path=V1HostPathVolumeSource(
                path="{{inputs.parameters.dflow_vol_path}}",
                type="DirectoryOrCreate"))
            mount = V1VolumeMount(name="mnt", mount_path="/mnt")
            script = ""
            for art in template.outputs.artifacts.values():
                script += "mkdir -p `dirname %s` && cp -r /mnt/workdir/%s"\
                    " %s\n" % (art.path, art.path, art.path)
            for par in template.outputs.parameters.values():
                if par.value_from_path is not None:
                    script += "mkdir -p `dirname %s` && cp -r /mnt/workdir/%s"\
                        " %s\n" % (par.value_from_path, par.value_from_path,
                                   par.value_from_path)
            collect_template = ShellOPTemplate(
                name=new_template.name + "-collect", image=self.collect_image,
                script=script, volumes=[volume], mounts=[mount])
            collect_template.inputs.parameters["dflow_vol_path"] = \
                InputParameter()
            for name in template.inputs.parameters.keys():
                if name[:6] == "dflow_":
                    collect_template.inputs.parameters[name] = \
                        InputParameter(
                        value="{{inputs.parameters.%s}}" % name)
            collect_template.outputs.parameters = copy.deepcopy(
                template.outputs.parameters)
            collect_template.outputs.artifacts = copy.deepcopy(
                template.outputs.artifacts)
            collect_step = Step("slurm-collect", template=collect_template,
                                parameters={
                                    "dflow_vol_path":
                                    run_step.outputs.parameters[
                                        "dflow_vol_path"]})
            new_template.add(collect_step)

            for art_name in template.outputs.artifacts:
                new_template.outputs.artifacts[art_name] = OutputArtifact(
                    name=art_name,
                    _from="{{steps.slurm-collect.outputs.artifacts.%s}}" %
                    art_name)
            for par_name in template.outputs.parameters:
                new_template.outputs.parameters[par_name] = OutputParameter(
                    name=par_name,
                    value_from_parameter="{{steps.slurm-collect.outputs"
                    ".parameters.%s}}" % par_name)

        return new_template


class SlurmRemoteExecutor(RemoteExecutor):
    """
    Slurm remote executor

    Args:
        host: remote host
        port: SSH port
        username: username
        password: password for SSH
        private_key_file: private key file for SSH
        workdir: remote working directory
        command: command for the executor
        remote_command: command for running the script remotely
        image: image for the executor
        map_tmp_dir: map /tmp to ./tmp
        docker_executable: docker executable to run remotely
        action_retries: retries for actions (upload, execute commands,
            download), -1 for infinity
        header: header for Slurm job
        interval: query interval for Slurm
    """

    def __init__(
            self,
            host: str,
            port: int = 22,
            username: str = "root",
            password: str = None,
            private_key_file: os.PathLike = None,
            workdir: str = "~/dflow/workflows/{{workflow.name}}/{{pod.name}}",
            command: Union[str, List[str]] = None,
            remote_command: Union[str, List[str]] = None,
            image: str = "dptechnology/dflow-extender",
            map_tmp_dir: bool = True,
            docker_executable: str = None,
            action_retries: int = -1,
            header: str = "",
            interval: int = 3,
            pvc: PVC = None,
    ) -> None:
        super().__init__(
            host=host, port=port, username=username, password=password,
            private_key_file=private_key_file, workdir=workdir,
            command=command, remote_command=remote_command, image=image,
            map_tmp_dir=map_tmp_dir, docker_executable=docker_executable,
            action_retries=action_retries)
        self.header = re.sub(" *#", "#", header)
        self.interval = interval
        self.pvc = pvc

    def run(self, image, remote_command):
        script = ""
        if self.docker_executable is None:
            map_cmd = "sed -i \"s#/tmp#$(pwd)/tmp#g\" script" if \
                self.map_tmp_dir else ""
            script += "echo '%s\n%s\n%s script' > slurm.sh\n" % (
                self.header, map_cmd, " ".join(remote_command))
        else:
            script += "echo '%s\n%s run -v$(pwd)/tmp:/tmp "\
                "-v$(pwd)/script:/script -ti %s %s /script' > slurm.sh\n" % (
                    self.header, self.docker_executable, image,
                    " ".join(remote_command))
        script += self.upload("slurm.sh", "%s/slurm.sh" %
                              self.workdir) + " || exit 1\n"
        if self.pvc:
            script += "echo 'jobIdFile: /mnt/job_id.txt' >> param.yaml\n"
        else:
            script += "echo 'jobIdFile: /tmp/job_id.txt' >> param.yaml\n"
        script += "echo 'workdir: %s' >> param.yaml\n" % self.workdir
        script += "echo 'scriptFile: slurm.sh' >> param.yaml\n"
        script += "echo 'interval: %s' >> param.yaml\n" % self.interval
        script += "echo 'host: %s' >> param.yaml\n" % self.host
        script += "echo 'port: %s' >> param.yaml\n" % self.port
        script += "echo 'username: %s' >> param.yaml\n" % self.username
        if self.password is not None:
            script += "echo 'password: %s' >> param.yaml\n" % self.password
        script += "./bin/slurm param.yaml || exit 1\n"
        return script

    def render(self, template):
        new_template = super().render(template)
        if self.pvc is not None:
            new_template.pvcs.append(self.pvc)
            new_template.mounts.append(V1VolumeMount(
                name=self.pvc.name, mount_path="/mnt",
                sub_path="{{pod.name}}"))
        return new_template
