"use client"

import api from "./client"

export type Board = {
  id: string
  title: string
  description: string
  content: any
  owner_id?: number
}

export type CreateBoardPayload = {
  title: string
  description: string
}

export type UpdateBoardPayload = {
  title: string
  description: string
  content: any
}

export async function getBoards(): Promise<Board[]> {
  const res = await api.get("/boards")
  return res.data
}

export async function getDiscoverBoards(): Promise<Board[]> {
  const res = await api.get("/boards/discover")
  return res.data
}

export async function getBoard(id: string): Promise<Board> {
  const res = await api.get(`/boards/${id}`)
  return res.data
}

export async function createBoard(payload: CreateBoardPayload): Promise<Board> {
  const res = await api.post("/boards", payload)
  return res.data
}

export async function deleteBoard(id: string) {
  const res = await api.delete(`/boards/${id}`)
  return res.data
}

export async function updateBoard(
  id: string,
  payload: {
    title: string;
    description: string;
    content: any;
  }
) {
  const res = await api.put(`/boards/${id}`, payload);
  return res.data;
}
