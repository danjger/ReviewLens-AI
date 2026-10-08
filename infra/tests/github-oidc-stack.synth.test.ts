/**
 * Synth checks for the GithubOidcStack (platform-foundation task 7.2).
 *
 * Requirements 1.3, 1.4, 8.7. The stack is the deploy pipeline's trust anchor:
 * an IAM OIDC provider for GitHub Actions plus a least-privilege role scoped to
 * one repo + branch. These tests confirm the stack synthesizes and that the
 * trust/permissions are shaped as intended (repo-scoped sub, bootstrap-role
 * assume), without needing a real deploy.
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { GithubOidcStack } from "../lib/github-oidc-stack";

function synth(props?: {
  githubOwner?: string;
  githubRepo?: string;
  createOidcProvider?: boolean;
}): Template {
  const app = new cdk.App();
  const stack = new GithubOidcStack(app, "TestOidc", {
    env: { account: "123456789012", region: "us-east-1" },
    ...props,
  });
  return Template.fromStack(stack);
}

test("creates an IAM OIDC provider for GitHub Actions by default", () => {
  const template = synth({ githubOwner: "my-org", githubRepo: "reviewlens" });
  // The provider is created via a custom resource; its URL/audience appear in
  // the custom-resource properties.
  template.hasResourceProperties("Custom::AWSCDKOpenIdConnectProvider", {
    Url: "https://token.actions.githubusercontent.com",
    ClientIDList: ["sts.amazonaws.com"],
  });
});

test("deploy role trusts only the configured repo on main and its environments", () => {
  const template = synth({ githubOwner: "my-org", githubRepo: "reviewlens" });
  template.hasResourceProperties("AWS::IAM::Role", {
    AssumeRolePolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: "sts:AssumeRoleWithWebIdentity",
          Condition: {
            StringEquals: {
              "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            },
            StringLike: {
              "token.actions.githubusercontent.com:sub": [
                "repo:my-org/reviewlens:ref:refs/heads/main",
                "repo:my-org/reviewlens:environment:*",
              ],
            },
          },
        }),
      ]),
    },
  });
});

test("deploy role can assume the CDK bootstrap roles (least-privilege pattern)", () => {
  const template = synth({ githubOwner: "my-org", githubRepo: "reviewlens" });
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: "sts:AssumeRole",
          Resource: Match.arrayWith([
            "arn:aws:iam::123456789012:role/cdk-hnb659fds-deploy-role-*",
            "arn:aws:iam::123456789012:role/cdk-hnb659fds-image-publishing-role-*",
          ]),
        }),
      ]),
    },
  });
});

test("exports the deploy role ARN as an output", () => {
  const template = synth({ githubOwner: "my-org", githubRepo: "reviewlens" });
  template.hasOutput("DeployRoleArn", {});
});

test("synthesizes with placeholder repo coordinates (no context supplied)", () => {
  // bin/app.ts falls back to placeholders so synth works without the real repo.
  const template = synth();
  template.hasResourceProperties("AWS::IAM::Role", {
    AssumeRolePolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Condition: {
            StringLike: {
              "token.actions.githubusercontent.com:sub": Match.arrayWith([
                "repo:OWNER-PLACEHOLDER/REPO-PLACEHOLDER:ref:refs/heads/main",
              ]),
            },
          },
        }),
      ]),
    },
  });
});

test("references an existing OIDC provider when createOidcProvider is false", () => {
  const template = synth({
    githubOwner: "my-org",
    githubRepo: "reviewlens",
    createOidcProvider: false,
  });
  // No custom-resource provider is created when we reuse the account's existing
  // GitHub OIDC provider.
  template.resourceCountIs("Custom::AWSCDKOpenIdConnectProvider", 0);
});
