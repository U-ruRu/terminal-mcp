import type { ManagedAccessPolicyReadModel } from '../api/models'
import type { MeshAccessPolicyRequest } from '../routes/MeshAccessPolicyControls'

export function meshPolicyMatches(policy: ManagedAccessPolicyReadModel | undefined, request: MeshAccessPolicyRequest): boolean {
  return Boolean(policy && policy.revision > request.expected_revision && policy.durationSeconds === request.duration_seconds
    && policy.warningAfterSeconds === request.warning_after_seconds && policy.alertAfterSeconds === request.alert_after_seconds
    && policy.rearmAfterSeconds === request.rearm_after_seconds)
}
