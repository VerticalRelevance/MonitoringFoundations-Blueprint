import os.path
import yaml
from aws_cdk.aws_s3_assets import Asset
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ssm as ssm,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as codepipeline_actions,
    Tags,
    App, Stack, RemovalPolicy, SecretValue
)

from constructs import Construct

dirname = os.path.dirname(__file__)

roleArn = None


class EC2InstanceCloudwatchRepaveStack(Stack):

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # VPC
        vpc = ec2.Vpc(self, "VPC",
                      nat_gateways=0,
                      subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)]
                      )

        # AMI
        amzn_linux = ec2.MachineImage.latest_amazon_linux(
            generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2,
            edition=ec2.AmazonLinuxEdition.STANDARD,
            virtualization=ec2.AmazonLinuxVirt.HVM,
            storage=ec2.AmazonLinuxStorage.GENERAL_PURPOSE
        )

        # Instance Role and SSM Managed Policy
        role = iam.Role(self, "InstanceSSM", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"))

        global roleArn
        roleArn = role

        # Instance
        instance = ec2.Instance(self, "CloudwatchRepaveTarget",
                                instance_type=ec2.InstanceType("t3.nano"),
                                machine_image=amzn_linux,
                                vpc=vpc,
                                role=role
                                )
        Tags.of(instance).add("Environment", "CWAgentRepave00")

        # User Data Script in S3 as Asset
        user_data_script = Asset(self, "UserDataAsset", path=os.path.join(dirname, "user_data.sh"))
        local_path_user_data_script = instance.user_data.add_s3_download_command(
            bucket=user_data_script.bucket,
            bucket_key=user_data_script.s3_object_key
        )

        # Userdata executes script from S3
        instance.user_data.add_execute_file_command(
            file_path=local_path_user_data_script
        )
        user_data_script.grant_read(instance.role)


class CodebuildPipeline(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        agent_config_bucket = s3.Bucket(
            self,
            'vr-labs-cloudwatch-agent-config',
            bucket_name='vr-labs-cloudwatch-agent-config',
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        global roleArn

        agent_config_bucket.add_to_resource_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["s3:PutObject",
                     "s3:GetObject"],
            resources=["arn:aws:s3:::vr-labs-cloudwatch-agent-config/*"],
            principals=[iam.ServicePrincipal("codebuild.amazonaws.com"),
                        iam.ServicePrincipal("ec2.amazonaws.com"),
                        iam.ArnPrincipal(roleArn.role_arn)]
        ));

        source_output = codepipeline.Artifact()
        source_action = codepipeline_actions.GitHubSourceAction(
            action_name="Source",
            owner="VerticalRelevance",
            repo="MonitoringFoundations-Blueprint-agent-config",
            branch="main",
            oauth_token=SecretValue.secrets_manager('github/personal/mhiggins', json_field="my-github-token"),
            output=source_output,
        )
        pipeline = codepipeline.Pipeline(
            self,
            "MonitoringBlueprintAgentConfigPipeline",
            stages=[
                codepipeline.StageProps(stage_name="Source", actions=[source_action])
            ],
        )

        codebuild_execution_policy_name = "Codebuild-Execution-Policy"
        CodeBuildExecutionPolicy = iam.ManagedPolicy(
            self, codebuild_execution_policy_name,
            managed_policy_name=codebuild_execution_policy_name,
            statements=[
                iam.PolicyStatement(effect=
                                    iam.Effect.ALLOW,
                                    actions=[
                                        "logs:CreateLogGroup",
                                        "logs:CreateLogStream",
                                        "logs:PutLogEvents",
                                        "sns:Publish",
                                        "s3:PutObject",
                                        "s3:GetObject",
                                        "s3:GetObjectVersion",
                                        "s3:GetBucketAcl",
                                        "s3:GetBucketLocation",
                                        "codebuild:CreateReportGroup",
                                        "codebuild:CreateReport",
                                        "codebuild:UpdateReport",
                                        "codebuild:BatchPutTestCases",
                                        "codebuild:BatchPutCodeCoverages",
                                        "ssm:SendCommand"
                                    ],
                                    resources=["*"],
                                    )
            ]
        )

        codebuild_execution_role_name = "Codebuild-Execution-role"
        CodebuildRole = iam.Role(
            self, codebuild_execution_role_name,
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            managed_policies=[CodeBuildExecutionPolicy],
            path=None,
            role_name=codebuild_execution_role_name
        )
        cloudwatch_agent_deploy = codebuild.PipelineProject(
            self,
            "Cloudwatch Agent Config Deploy",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.AMAZON_LINUX_2_2,

            ),
            role=CodebuildRole,
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "build": {
                            "commands": ['aws s3 cp config.json s3://vr-labs-cloudwatch-agent-config/',
                                         'aws ssm send-command --document-name "ssm_document_cloudwatch_agent" --document-version "1" --targets \'[{"Key":"tag:Environment","Values":["CWAgentRepave00"]}]\' --parameters \'{}\' --timeout-seconds 600 --max-concurrency "50" --max-errors "0" --region us-east-1'
                                         ],
                        }
                    }
                }
            )
        )

        pipeline.add_stage(
            stage_name="Deploy_Cloudwatch_Agent_Config",
            actions=[
                codepipeline_actions.CodeBuildAction(
                    action_name="Cloudwatch-Config-Deploy",
                    project=cloudwatch_agent_deploy,
                    input=source_output,
                )
            ],
        )


class SSMRepaveDocument(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        with open("ssm_document_cloudwatch_agent.yml", 'r') as file:
            content = yaml.safe_load(file)
            cfn_document = ssm.CfnDocument(self, "MyCfnDocument",
                                           content=content,
                                           document_type="Command",
                                           name="ssm_document_cloudwatch_agent",
                                           )


app = App()
EC2InstanceCloudwatchRepaveStack(app, "ec2-instance-cloudwatch-repave")
CodebuildPipeline(app, "codebuild-pipeline")
SSMRepaveDocument(app, "SSMRepaveDocument")
app.synth()
