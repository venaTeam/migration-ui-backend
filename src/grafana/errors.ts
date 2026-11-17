export class GrafanaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GrafanaError";
  }
}

export class GrafanaAuthError extends GrafanaError {
  constructor(message = "Invalid Grafana credentials") {
    super(message);
    this.name = "GrafanaAuthError";
  }
}

export class GrafanaAPIError extends GrafanaError {
  constructor(message: string, public statusCode: number) {
    super(message);
    this.name = "GrafanaAPIError";
  }
}

export class RuleNotFoundError extends GrafanaError {
  constructor(uid: string) {
    super(`Rule '${uid}' not found or not accessible`);
    this.name = "RuleNotFoundError";
  }
}
