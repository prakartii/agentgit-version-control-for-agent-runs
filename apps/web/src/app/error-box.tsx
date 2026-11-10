import { ApiError } from "@/lib/api";

export function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  return <p className="error">{message}</p>;
}
