/**
 * CostStack – the account-level cost guardrail for ReviewLens AI.
 *
 * platform-foundation task 6.7. Requirement 7.5: "The deployment SHALL include
 * a monthly cloud budget alarm, and the AI provider account SHALL have a spend
 * limit set. Both are documented in the README." The design's Error Handling
 * section lists, under abuse/cost protection, "an AWS Budgets alarm, and a
 * spend limit in the Anthropic console."
 *
 * This stack owns the AWS side of that control: a monthly AWS Budgets COST
 * budget that emails a subscriber when ACTUAL spend crosses a threshold.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * ACCOUNT-SCOPED: AWS Budgets is an account-level service (budgets are not
 * regional resources). This stack therefore defines the budget ONCE per
 * account; it should not be instantiated per-region. bin/app.ts creates it in
 * the standard CDK environment (same account/region pattern as the other
 * stacks) for consistency, but the region is immaterial to the budget itself.
 *
 * NOT AUTOMATED HERE: the Anthropic (AI provider) spend limit is set manually
 * in the Anthropic console — there is no CDK/CloudFormation resource for it.
 * That is a "needs a person" item documented in the README (task 1) and
 * referenced by the deploy runbook (task 7.2). Do not attempt to automate it.
 *
 * EMAIL SUBSCRIBER: the alert email is NOT hardcoded. It comes from the
 * `alertEmail` prop (wired from the `budgetAlertEmail` context value in
 * bin/app.ts). When no email is supplied, the budget is still created but WITH
 * NO notification/subscriber, so `cdk synth` always succeeds without a
 * placeholder address. A real deployment supplies the email via context, e.g.
 *   cdk deploy -c budgetAlertEmail=ops@example.com
 *
 * Requirements: 7.5
 */
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as budgets from "aws-cdk-lib/aws-budgets";

/** Default monthly budget limit (USD) when none is supplied via context. */
export const DEFAULT_MONTHLY_BUDGET_USD = 100;

export interface CostStackProps extends cdk.StackProps {
  /**
   * Monthly budget limit in USD. Defaults to {@link DEFAULT_MONTHLY_BUDGET_USD}.
   * Wired from the `monthlyBudgetUsd` context value in bin/app.ts.
   */
  readonly monthlyBudgetUsd?: number;

  /**
   * Email address that receives the budget alert. Wired from the
   * `budgetAlertEmail` context value in bin/app.ts. When omitted, the budget is
   * created with no notification/subscriber so `cdk synth` succeeds without a
   * placeholder address; supply it at deploy time to enable the alert.
   */
  readonly alertEmail?: string;
}

export class CostStack extends cdk.Stack {
  /** The monthly cost budget. */
  public readonly budget: budgets.CfnBudget;

  constructor(scope: Construct, id: string, props: CostStackProps = {}) {
    super(scope, id, props);

    const limitUsd = props.monthlyBudgetUsd ?? DEFAULT_MONTHLY_BUDGET_USD;
    const alertEmail = props.alertEmail;

    // ------------------------------------------------------------------
    // Notification subscribers (Requirement 7.5)
    // ------------------------------------------------------------------
    // Alert at 80% (forecast of overrun / early warning) and at 100% of ACTUAL
    // spend, each emailing the supplied subscriber. When no email is supplied
    // we attach NO notifications, so synth never needs a placeholder address;
    // the budget alone still records the monthly cap.
    const notificationsWithSubscribers:
      | budgets.CfnBudget.NotificationWithSubscribersProperty[]
      | undefined = alertEmail
      ? [80, 100].map((threshold) => ({
          notification: {
            notificationType: "ACTUAL",
            comparisonOperator: "GREATER_THAN",
            threshold,
            thresholdType: "PERCENTAGE",
          },
          subscribers: [
            {
              subscriptionType: "EMAIL",
              address: alertEmail,
            },
          ],
        }))
      : undefined;

    // ------------------------------------------------------------------
    // Monthly cost budget (account-scoped — defined once per account)
    // ------------------------------------------------------------------
    this.budget = new budgets.CfnBudget(this, "MonthlyCostBudget", {
      budget: {
        budgetName: `${this.stackName}-monthly-cost`,
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: {
          amount: limitUsd,
          unit: "USD",
        },
      },
      notificationsWithSubscribers,
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, "BudgetName", {
      value: `${this.stackName}-monthly-cost`,
      description: "Name of the monthly AWS Budgets cost budget",
    });
  }
}
